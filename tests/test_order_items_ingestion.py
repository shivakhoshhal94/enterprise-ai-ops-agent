import csv
import io
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql.dml import Insert

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.models import OrderItem
from enterprise_ai_ops_agent.ingestion import order_items as loader


def sample_row():
    return ['order1', '1', 'product1', 'seller1', '2017-10-18 00:00:00', '19.90', '0.00']


def csv_text(rows, header=loader.SOURCE_COLUMNS):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue()


def test_valid_values_and_preservation():
    row = sample_row()
    row[0] = ' order1 '
    parsed = loader.parse_order_items_csv(io.StringIO(csv_text([row])))[0]
    assert parsed == {'order_id': ' order1 ', 'order_item_id': 1, 'product_id': 'product1',
                      'seller_id': 'seller1', 'shipping_limit_date': datetime(2017,10,18),
                      'price': Decimal('19.90'), 'freight_value': Decimal('0.00')}
    assert isinstance(parsed['price'], Decimal)
    assert isinstance(parsed['freight_value'], Decimal)
    assert parsed['shipping_limit_date'].tzinfo is None


@pytest.mark.parametrize('header', [loader.SOURCE_COLUMNS[:-1], ('order_id',)*7, loader.SOURCE_COLUMNS+('extra',)])
def test_headers(header):
    with pytest.raises(loader.OrderItemsIngestionError, match='header'):
        loader.parse_order_items_csv(io.StringIO(csv_text([], header)))


@pytest.mark.parametrize('index', range(7))
@pytest.mark.parametrize('value', ['', '   '])
def test_required_fields(index, value):
    row = sample_row()
    row[index] = value
    with pytest.raises(loader.OrderItemsIngestionError, match='Blank'):
        loader.parse_order_items_csv(io.StringIO(csv_text([row])))


def test_composite_keys():
    first = sample_row()
    second = first.copy(); second[0] = 'order2'
    third = first.copy(); third[1] = '2'
    assert len(loader.parse_order_items_csv(io.StringIO(csv_text([first,second,third])))) == 3
    equivalent = first.copy(); equivalent[1] = '1.0'
    with pytest.raises(loader.OrderItemsIngestionError, match='Duplicate'):
        loader.parse_order_items_csv(io.StringIO(csv_text([first,equivalent])))


@pytest.mark.parametrize('value,expected', [('1',1), ('2.0',2), ('1e2',100), ('2147483647',2147483647)])
def test_integer_parsing(value, expected):
    assert loader.parse_item_id(value, 2) == expected


@pytest.mark.parametrize('value', ['0','-1','1.5','bad','NaN','Infinity','1_0','2147483648','1e999','1e999999999999999999999999999999'])
def test_invalid_integer(value):
    with pytest.raises(loader.OrderItemsIngestionError):
        loader.parse_item_id(value, 2)


@pytest.mark.parametrize('value', ['0','0.00','19.90','0.01','1e2','9999999999.99'])
def test_money_exact(value):
    assert loader.parse_money(value,'price',2) == Decimal(value)


@pytest.mark.parametrize('value', ['bad','NaN','Infinity','1_000','0.001','10000000000','1e999','1e999999999999999999999999999999'])
@pytest.mark.parametrize('column', ['price','freight_value'])
def test_invalid_money(value, column):
    with pytest.raises(loader.OrderItemsIngestionError):
        loader.parse_money(value,column,2)


@pytest.mark.parametrize('value', ['bad','2017-02-30 00:00:00','2017-1-01 00:00:00','2017-01-01T00:00:00','2017-01-01 00:00:00Z'])
def test_malformed_deadline(value):
    with pytest.raises(loader.OrderItemsIngestionError,match='shipping_limit_date'):
        loader.parse_deadline(value,2)


def test_outlier_deadline():
    row=sample_row(); row[4]='2020-04-09 22:35:08'
    assert loader.parse_order_items_csv(io.StringIO(csv_text([row])))[0]['shipping_limit_date'] == datetime(2020,4,9,22,35,8)


@pytest.mark.parametrize('rows', [[], [['few']], [sample_row()+['extra']]])
def test_structure(rows):
    with pytest.raises(loader.OrderItemsIngestionError):
        loader.parse_order_items_csv(io.StringIO(csv_text(rows)))


@pytest.mark.parametrize('missing', [None, 'order_id', 'product_id', 'seller_id', 'all'])
def test_parent_validation_three_queries(missing):
    connection=MagicMock()
    results=[]
    for name,value in [('order_id','order1'),('product_id','product1'),('seller_id','seller1')]:
        result=MagicMock(); result.scalars.return_value=[] if missing in {name,'all'} else [value]
        results.append(result)
    connection.execute.side_effect=results
    rows=[{'order_id':'order1','product_id':'product1','seller_id':'seller1'} for _ in range(100)]
    if missing:
        with pytest.raises(loader.OrderItemsIngestionError,match='1 distinct unmatched') as caught:
            loader.validate_parent_ids(connection,rows)
        if missing != 'all': assert missing in str(caught.value)
        else: assert all(name in str(caught.value) for name in ['order_id','product_id','seller_id'])
    else:
        loader.validate_parent_ids(connection,rows)
    assert connection.execute.call_count == 3


@pytest.mark.parametrize('case', ['success','missing','nonempty','unmatched','late_failure','mismatch'])
def test_transaction_and_batches(monkeypatch,tmp_path,case):
    second=sample_row(); second[1]='2'
    path=tmp_path/'items.csv'; path.write_text(csv_text([sample_row(),second]),encoding='utf-8',newline='')
    engine=MagicMock(spec=Engine)
    connection=engine.begin.return_value.__enter__.return_value
    inspector=MagicMock(); inspector.has_table.return_value=case!='missing'
    monkeypatch.setattr(loader,'inspect',lambda c:inspector)
    empty=MagicMock(); empty.scalar_one.return_value=1 if case=='nonempty' else 0
    parents=[]
    for value in ['order1','product1','seller1']:
        result=MagicMock(); result.scalars.return_value=[] if case=='unmatched' else [value]
        parents.append(result)
    final=MagicMock(); final.scalar_one.return_value=1 if case=='mismatch' else 2
    last=OperationalError(None,None,Exception('secret_password')) if case=='late_failure' else MagicMock()
    connection.execute.side_effect=[empty,*parents,MagicMock(),last,final]
    if case=='success':
        assert loader.ingest_order_items(engine,path,1)=={'source_rows_read':2,'rows_inserted':2,'target_table':'order_items'}
        inserts=[call for call in connection.execute.call_args_list if isinstance(call.args[0],Insert)]
        assert len(inserts)==2
        assert all(call.args[0].table is OrderItem.__table__ for call in inserts)
        assert inserts[0].args[1][0]['freight_value']==Decimal('0.00')
        engine.begin.return_value.__exit__.assert_called_once_with(None,None,None)
    else:
        with pytest.raises(loader.OrderItemsIngestionError) as caught:
            loader.ingest_order_items(engine,path,1)
        assert 'secret_password' not in str(caught.value)
        assert engine.begin.return_value.__exit__.call_args.args[0] is not None
        if case in {'missing','nonempty','unmatched'}:
            assert not any(isinstance(call.args[0],Insert) for call in connection.execute.call_args_list)


def test_real_rollback_without_postgres(monkeypatch,tmp_path):
    engine=create_engine('sqlite://')
    OrderItem.__table__.create(engine)
    monkeypatch.setattr(loader,'validate_parent_ids',lambda c,rows:None)
    second=sample_row(); second[1]='2'
    path=tmp_path/'items.csv'; path.write_text(csv_text([sample_row(),second]),encoding='utf-8',newline='')
    def fail_after_first_batch(c,t,rows,batch_size,error_type):
        c.execute(t.insert(),rows[:1])
        raise OperationalError(None,None,Exception('secret_password'))
    monkeypatch.setattr(loader,'insert_reconciled_rows',fail_after_first_batch)
    with pytest.raises(loader.OrderItemsIngestionError,match='rolled back'):
        loader.ingest_order_items(engine,path,1)
    with engine.connect() as c:
        assert c.execute(OrderItem.__table__.select()).all()==[]
    engine.dispose()


def test_bad_source_never_connects(tmp_path):
    path=tmp_path/'bad.csv'; path.write_text('bad\n',encoding='utf-8')
    engine=MagicMock(spec=Engine)
    with pytest.raises(loader.OrderItemsIngestionError):
        loader.ingest_order_items(engine,path)
    engine.begin.assert_not_called()


@pytest.mark.parametrize('case',['missing','invalid','database','success'])
def test_cli_safety(monkeypatch,capsys,case):
    settings=Settings(_env_file=None,database_url=None if case=='missing' else 'postgresql+psycopg://localhost/test')
    monkeypatch.setattr(loader,'Settings',lambda:settings)
    factory=MagicMock(return_value=MagicMock(spec=Engine))
    if case=='invalid': factory.side_effect=ValueError('secret_password')
    monkeypatch.setattr(loader,'create_db_engine',factory)
    operation=MagicMock(return_value={'source_rows_read':2,'rows_inserted':2,'target_table':'order_items'})
    if case=='database': operation.side_effect=loader.OrderItemsIngestionError('Order items ingestion failed.')
    monkeypatch.setattr(loader,'ingest_order_items',operation)
    assert loader.main()==(0 if case=='success' else 1)
    output=capsys.readouterr()
    assert 'secret_password' not in output.out+output.err
    if case in {'database','success'}: factory.return_value.dispose.assert_called_once()
    else: operation.assert_not_called()
