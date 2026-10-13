import csv
import io
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql.dml import Insert

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.models import OrderPayment
from enterprise_ai_ops_agent.ingestion import order_payments as loader


def sample_row():
    return ['order1', '1', 'credit_card', '8', '99.33']


def csv_text(rows, header=loader.SOURCE_COLUMNS):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue()


def test_valid_payment_preserves_amount_without_installment_multiplier():
    parsed = loader.parse_order_payments_csv(io.StringIO(csv_text([sample_row()])))[0]
    assert parsed == {'order_id':'order1','payment_sequential':1,'payment_type':'credit_card',
                      'payment_installments':8,'payment_value':Decimal('99.33')}
    assert isinstance(parsed['payment_value'], Decimal)


@pytest.mark.parametrize('header', [loader.SOURCE_COLUMNS[:-1], ('order_id',)*5, loader.SOURCE_COLUMNS+('extra',)])
def test_headers(header):
    with pytest.raises(loader.OrderPaymentsIngestionError, match='header'):
        loader.parse_order_payments_csv(io.StringIO(csv_text([], header)))


@pytest.mark.parametrize('index', range(5))
@pytest.mark.parametrize('value', ['', '   '])
def test_required_fields(index, value):
    row = sample_row()
    row[index] = value
    with pytest.raises(loader.OrderPaymentsIngestionError, match='Blank'):
        loader.parse_order_payments_csv(io.StringIO(csv_text([row])))


def test_composite_identity():
    first = sample_row()
    second = first.copy(); second[0] = 'order2'
    third = first.copy(); third[1] = '2'
    assert len(loader.parse_order_payments_csv(io.StringIO(csv_text([first,second,third])))) == 3
    equivalent = first.copy(); equivalent[1] = '1.0'
    with pytest.raises(loader.OrderPaymentsIngestionError, match='Duplicate'):
        loader.parse_order_payments_csv(io.StringIO(csv_text([first,equivalent])))


@pytest.mark.parametrize('column',['payment_sequential','payment_installments'])
@pytest.mark.parametrize('value,expected',[('0',0),('1',1),('8.0',8),('1e2',100),('2147483647',2147483647)])
def test_integer_conversion(column,value,expected):
    assert loader.parse_integer(value,column,2)==expected


@pytest.mark.parametrize('column',['payment_sequential','payment_installments'])
@pytest.mark.parametrize('value',['bad','1.5','NaN','Infinity','1_0','2147483648','-2147483649','1e999','1e9999999999999999999999999'])
def test_invalid_integer(column,value):
    with pytest.raises(loader.OrderPaymentsIngestionError):
        loader.parse_integer(value,column,2)


@pytest.mark.parametrize('payment_type',['not_defined',' credit_card ','voucher'])
def test_types_and_zeros_preserved(payment_type):
    row=sample_row(); row[2]=payment_type; row[3]='0'; row[4]='0.00'
    parsed=loader.parse_order_payments_csv(io.StringIO(csv_text([row])))[0]
    assert parsed['payment_type']==payment_type
    assert parsed['payment_installments']==0
    assert parsed['payment_value']==Decimal('0.00')


@pytest.mark.parametrize('value',['bad','NaN','Infinity','1_000','0.001','10000000000','1e999','1e9999999999999999999999999'])
def test_invalid_money(value):
    with pytest.raises(loader.OrderPaymentsIngestionError):
        loader.parse_money(value,'payment_value',2)


@pytest.mark.parametrize('rows',[[],[['few']],[sample_row()+['extra']]])
def test_structure(rows):
    with pytest.raises(loader.OrderPaymentsIngestionError):
        loader.parse_order_payments_csv(io.StringIO(csv_text(rows)))


@pytest.mark.parametrize('missing',[False,True])
def test_parent_validation_is_one_query(missing):
    connection=MagicMock()
    connection.execute.return_value.scalars.return_value=[] if missing else ['order1']
    rows=[{'order_id':'order1'} for _ in range(100)]
    if missing:
        with pytest.raises(loader.OrderPaymentsIngestionError,match='1 distinct') as caught:
            loader.validate_order_ids(connection,rows)
        assert 'order1' in str(caught.value)
    else:
        loader.validate_order_ids(connection,rows)
    connection.execute.assert_called_once()
    assert list(connection.execute.call_args.args[0].selected_columns)[0].name=='order_id'


@pytest.mark.parametrize('case',['success','missing','nonempty','unmatched','late_failure','mismatch'])
def test_transaction_and_batches(monkeypatch,tmp_path,case):
    second=sample_row(); second[1]='2'
    path=tmp_path/'payments.csv'
    path.write_text(csv_text([sample_row(),second]),encoding='utf-8',newline='')
    engine=MagicMock(spec=Engine)
    connection=engine.begin.return_value.__enter__.return_value
    inspector=MagicMock(); inspector.has_table.return_value=case!='missing'
    monkeypatch.setattr(loader,'inspect',lambda c:inspector)
    empty=MagicMock(); empty.scalar_one.return_value=1 if case=='nonempty' else 0
    parents=MagicMock(); parents.scalars.return_value=[] if case=='unmatched' else ['order1']
    final=MagicMock(); final.scalar_one.return_value=1 if case=='mismatch' else 2
    last=OperationalError(None,None,Exception('secret_password')) if case=='late_failure' else MagicMock()
    connection.execute.side_effect=[empty,parents,MagicMock(),last,final]
    if case=='success':
        assert loader.ingest_order_payments(engine,path,1)=={'source_rows_read':2,'rows_inserted':2,'target_table':'order_payments'}
        inserts=[call for call in connection.execute.call_args_list if isinstance(call.args[0],Insert)]
        assert len(inserts)==2
        assert all(call.args[0].table is OrderPayment.__table__ for call in inserts)
        assert inserts[0].args[1][0]['payment_value']==Decimal('99.33')
        engine.begin.return_value.__exit__.assert_called_once_with(None,None,None)
    else:
        with pytest.raises(loader.OrderPaymentsIngestionError) as caught:
            loader.ingest_order_payments(engine,path,1)
        assert 'secret_password' not in str(caught.value)
        assert engine.begin.return_value.__exit__.call_args.args[0] is not None
        if case in {'missing','nonempty','unmatched'}:
            assert not any(isinstance(call.args[0],Insert) for call in connection.execute.call_args_list)


def test_actual_rollback_without_postgres(monkeypatch,tmp_path):
    engine=create_engine('sqlite://')
    OrderPayment.__table__.create(engine)
    monkeypatch.setattr(loader,'validate_order_ids',lambda c,rows:None)
    path=tmp_path/'payments.csv'
    path.write_text(csv_text([sample_row()]),encoding='utf-8',newline='')
    def fail_after_insert(c,t,rows,batch_size,error_type):
        c.execute(t.insert(),rows)
        raise OperationalError(None,None,Exception('secret_password'))
    monkeypatch.setattr(loader,'insert_reconciled_rows',fail_after_insert)
    with pytest.raises(loader.OrderPaymentsIngestionError,match='rolled back'):
        loader.ingest_order_payments(engine,path)
    with engine.connect() as c:
        assert c.execute(OrderPayment.__table__.select()).all()==[]
    engine.dispose()


def test_bad_source_never_connects(tmp_path):
    path=tmp_path/'bad.csv'; path.write_text('bad\n',encoding='utf-8')
    engine=MagicMock(spec=Engine)
    with pytest.raises(loader.OrderPaymentsIngestionError):
        loader.ingest_order_payments(engine,path)
    engine.begin.assert_not_called()


@pytest.mark.parametrize('case',['missing','invalid','database','success'])
def test_cli_safety(monkeypatch,capsys,case):
    settings=Settings(_env_file=None,database_url=None if case=='missing' else 'postgresql+psycopg://localhost/test')
    monkeypatch.setattr(loader,'Settings',lambda:settings)
    factory=MagicMock(return_value=MagicMock(spec=Engine))
    if case=='invalid': factory.side_effect=ValueError('secret_password')
    monkeypatch.setattr(loader,'create_db_engine',factory)
    operation=MagicMock(return_value={'source_rows_read':2,'rows_inserted':2,'target_table':'order_payments'})
    if case=='database': operation.side_effect=loader.OrderPaymentsIngestionError('Payments ingestion failed.')
    monkeypatch.setattr(loader,'ingest_order_payments',operation)
    assert loader.main()==(0 if case=='success' else 1)
    output=capsys.readouterr()
    assert 'secret_password' not in output.out+output.err
    if case in {'database','success'}: factory.return_value.dispose.assert_called_once()
    else: operation.assert_not_called()
