import csv
import io
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql.dml import Insert

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.models import Customer, Order
from enterprise_ai_ops_agent.ingestion import orders


def sample_row():
    return ['order1', 'customer1', ' delivered ', '2017-10-02 10:56:33',
            '2017-10-02 11:07:15', '2017-10-04 19:55:00',
            '2017-10-10 21:25:13', '2017-10-18 00:00:00']


def csv_text(rows, header=orders.SOURCE_COLUMNS):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue()


def test_valid_preserved_naive_timestamps():
    parsed = orders.parse_orders_csv(io.StringIO(csv_text([sample_row()])))[0]
    assert parsed['order_status'] == ' delivered '
    for name, value in zip(orders.SOURCE_COLUMNS[3:], sample_row()[3:]):
        assert parsed[name] == datetime.strptime(value, orders.TIMESTAMP_FORMAT)
        assert parsed[name].tzinfo is None


@pytest.mark.parametrize('header', [orders.SOURCE_COLUMNS[:-1], ('order_id',)*8, orders.SOURCE_COLUMNS+('extra',)])
def test_headers(header):
    with pytest.raises(orders.OrdersIngestionError, match='header'):
        orders.parse_orders_csv(io.StringIO(csv_text([], header)))


@pytest.mark.parametrize('index', [0, 1, 2, 3, 7])
@pytest.mark.parametrize('value', ['', '   '])
def test_required_nonblank(index, value):
    row=sample_row(); row[index]=value
    with pytest.raises(orders.OrdersIngestionError, match='Blank'):
        orders.parse_orders_csv(io.StringIO(csv_text([row])))


def test_duplicate_order():
    with pytest.raises(orders.OrdersIngestionError, match='Duplicate'):
        orders.parse_orders_csv(io.StringIO(csv_text([sample_row(), sample_row()])))


@pytest.mark.parametrize('index', [4, 5, 6])
def test_nullable_timestamp(index):
    row=sample_row(); row[index]=''
    assert orders.parse_orders_csv(io.StringIO(csv_text([row])))[0][orders.SOURCE_COLUMNS[index]] is None


@pytest.mark.parametrize('value', ['bad', '2017-02-30 00:00:00', '2017-1-02 10:56:33', '2017-10-02T10:56:33', '2017-10-02 10:56:33Z', ' 2017-10-02 10:56:33', '2017-10-02 10:56:33.0'])
@pytest.mark.parametrize('index', [3, 4, 5, 6, 7])
def test_malformed_timestamp(index, value):
    row=sample_row(); row[index]=value
    with pytest.raises(orders.OrdersIngestionError, match='Invalid timestamp'):
        orders.parse_orders_csv(io.StringIO(csv_text([row])))


def test_anomalies_and_repeated_customer_are_allowed():
    row=sample_row(); row[5]='2017-10-01 00:00:00'; row[6]='2017-09-30 00:00:00'
    second=row.copy(); second[0]='order2'
    assert len(orders.parse_orders_csv(io.StringIO(csv_text([row,second])))) == 2


@pytest.mark.parametrize('rows', [[], [['few']], [sample_row()+['extra']]])
def test_structure(rows):
    with pytest.raises(orders.OrdersIngestionError):
        orders.parse_orders_csv(io.StringIO(csv_text(rows)))


@pytest.mark.parametrize('missing', [False, True])
def test_fk_one_query_and_bounded_sample(missing):
    connection=MagicMock()
    connection.execute.return_value.scalars.return_value=['customer1']
    rows=[{'customer_id':'customer1'} for _ in range(100)]
    if missing:
        rows += [{'customer_id':'missing\ncustomer'}, {'customer_id':'missing\ncustomer'}]
        with pytest.raises(orders.OrdersIngestionError, match='1 distinct') as caught:
            orders.validate_customer_ids(connection, rows)
        assert '\n' not in str(caught.value)
        assert 'missing' in str(caught.value)
    else:
        orders.validate_customer_ids(connection, rows)
    connection.execute.assert_called_once()
    assert list(connection.execute.call_args.args[0].selected_columns)[0].name == 'customer_id'


@pytest.mark.parametrize('case', ['success', 'missing', 'nonempty', 'unmatched', 'late_failure', 'mismatch'])
def test_transaction_and_batches(monkeypatch, tmp_path, case):
    second=sample_row(); second[0]='order2'
    path=tmp_path/'orders.csv'; path.write_text(csv_text([sample_row(),second]),encoding='utf-8',newline='')
    engine=MagicMock(spec=Engine)
    connection=engine.begin.return_value.__enter__.return_value
    inspector=MagicMock(); inspector.has_table.return_value=case!='missing'
    monkeypatch.setattr(orders,'inspect',lambda c:inspector)
    empty=MagicMock(); empty.scalar_one.return_value=1 if case=='nonempty' else 0
    parents=MagicMock(); parents.scalars.return_value=[] if case=='unmatched' else ['customer1']
    final=MagicMock(); final.scalar_one.return_value=1 if case=='mismatch' else 2
    last=OperationalError(None,None,Exception('secret_password')) if case=='late_failure' else MagicMock()
    connection.execute.side_effect=[empty,parents,MagicMock(),last,final]
    if case=='success':
        assert orders.ingest_orders(engine,path,1)=={'source_rows_read':2,'rows_inserted':2,'target_table':'orders'}
        inserts=[call for call in connection.execute.call_args_list if isinstance(call.args[0],Insert)]
        assert len(inserts)==2
        assert all(call.args[0].table is Order.__table__ for call in inserts)
        assert isinstance(inserts[0].args[1][0]['order_purchase_timestamp'],datetime)
        engine.begin.return_value.__exit__.assert_called_once_with(None,None,None)
    else:
        with pytest.raises(orders.OrdersIngestionError) as caught:
            orders.ingest_orders(engine,path,1)
        assert 'secret_password' not in str(caught.value)
        assert engine.begin.return_value.__exit__.call_args.args[0] is not None
        if case in {'missing','nonempty','unmatched'}:
            assert not any(isinstance(call.args[0],Insert) for call in connection.execute.call_args_list)


def test_actual_transaction_rolls_back_late_batch(monkeypatch,tmp_path):
    # In-memory SQLite verifies rollback semantics without Docker/PostgreSQL.
    engine=create_engine('sqlite://')
    Customer.__table__.create(engine); Order.__table__.create(engine)
    with engine.begin() as c:
        c.execute(Customer.__table__.insert(),{'customer_id':'customer1','customer_unique_id':'u','customer_zip_code_prefix':'00001','customer_city':'city','customer_state':'SP'})
    second=sample_row(); second[0]='order2'
    path=tmp_path/'orders.csv'; path.write_text(csv_text([sample_row(),second]),encoding='utf-8',newline='')
    original=orders.insert_reconciled_rows
    def fail_after_insert(c,t,rows,batch_size,error_type):
        original(c,t,rows,batch_size,error_type)
        raise OperationalError(None,None,Exception('secret_password'))
    monkeypatch.setattr(orders,'insert_reconciled_rows',fail_after_insert)
    with pytest.raises(orders.OrdersIngestionError,match='rolled back'):
        orders.ingest_orders(engine,path,1)
    with engine.connect() as c:
        assert c.execute(Order.__table__.select()).all()==[]
    engine.dispose()


def test_invalid_source_never_connects(tmp_path):
    path=tmp_path/'bad.csv'; path.write_text('bad\n',encoding='utf-8',newline='')
    engine=MagicMock(spec=Engine)
    with pytest.raises(orders.OrdersIngestionError):
        orders.ingest_orders(engine,path)
    engine.begin.assert_not_called()


@pytest.mark.parametrize('case',['missing','invalid','database','success'])
def test_cli_safety(monkeypatch,capsys,case):
    settings=Settings(_env_file=None,database_url=None if case=='missing' else 'postgresql+psycopg://localhost/test')
    monkeypatch.setattr(orders,'Settings',lambda:settings)
    factory=MagicMock(return_value=MagicMock(spec=Engine))
    if case=='invalid': factory.side_effect=ValueError('secret_password')
    monkeypatch.setattr(orders,'create_db_engine',factory)
    operation=MagicMock(return_value={'source_rows_read':2,'rows_inserted':2,'target_table':'orders'})
    if case=='database': operation.side_effect=orders.OrdersIngestionError('Orders ingestion failed.')
    monkeypatch.setattr(orders,'ingest_orders',operation)
    assert orders.main()==(0 if case=='success' else 1)
    output=capsys.readouterr()
    assert 'secret_password' not in output.out+output.err
    if case in {'database','success'}: factory.return_value.dispose.assert_called_once()
    else: operation.assert_not_called()

