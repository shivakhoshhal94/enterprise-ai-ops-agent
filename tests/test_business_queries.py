from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects import postgresql

from enterprise_ai_ops_agent.db.base import Base
from enterprise_ai_ops_agent.db.models import Customer, Order, OrderItem, OrderPayment, Product, Seller
from enterprise_ai_ops_agent.business.customers import get_customer_order_history
from enterprise_ai_ops_agent.business.finance import get_order_financial_summary
from enterprise_ai_ops_agent.business.orders import get_order_status_summary


@pytest.fixture
def connection():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(Customer.__table__.insert(), [
            {'customer_id': name, 'customer_unique_id': identity, 'customer_zip_code_prefix': '00001',
             'customer_city': 'city', 'customer_state': 'SP'}
            for name, identity in [('c1','repeat'), ('c2','repeat'), ('c3','repeat'), ('other','separate')]
        ])
        connection.execute(Product.__table__.insert(), {'product_id': 'p'})
        connection.execute(Seller.__table__.insert(), [
            {'seller_id': name, 'seller_zip_code_prefix': '00001', 'seller_city': 'city', 'seller_state': 'SP'}
            for name in ['s1','s2']
        ])
    with engine.connect() as connection:
        yield connection
    engine.dispose()


def add_order(connection, order_id='order1', customer_id='c1', status='delivered', day=1):
    connection.execute(Order.__table__.insert(), {
        'order_id': order_id, 'customer_id': customer_id, 'order_status': status,
        'order_purchase_timestamp': datetime(2017,1,day),
        'order_estimated_delivery_date': datetime(2017,2,1),
    })


def add_children(connection, prices, payments, freight='0.00'):
    if prices:
        connection.execute(OrderItem.__table__.insert(), [
            {'order_id':'order1', 'order_item_id': index, 'product_id':'p',
             'seller_id': 's1' if index == 1 else 's2',
             'shipping_limit_date': datetime(2017,1,3), 'price':Decimal(price), 'freight_value':Decimal(freight)}
            for index, price in enumerate(prices,1)
        ])
    if payments:
        connection.execute(OrderPayment.__table__.insert(), [
            {'order_id':'order1', 'payment_sequential':index, 'payment_type':'credit_card',
             'payment_installments':12, 'payment_value':Decimal(value)}
            for index, value in enumerate(payments,1)
        ])


def test_status_grouping_and_deterministic_order(connection):
    add_order(connection,'z',status='shipped')
    add_order(connection,'a',status='delivered')
    add_order(connection,'b',status='delivered')
    assert [row.model_dump() for row in get_order_status_summary(connection)] == [
        {'order_status':'delivered','order_count':2}, {'order_status':'shipped','order_count':1},
    ]


def test_empty_status_summary(connection):
    assert get_order_status_summary(connection)==[]


def test_repeat_identity_and_history_order(connection):
    add_order(connection,'later','c1',day=2)
    add_order(connection,'first-b','c2',status='shipped')
    add_order(connection,'first-a','c2')
    add_order(connection,'unrelated','other')
    result=get_customer_order_history(connection,'repeat')
    assert result.found
    assert result.customer_record_count==3  # includes the customer record without an order
    assert result.order_count==3
    assert [o.order_id for o in result.orders]==['first-a','first-b','later']
    assert result.orders[1].order_status=='shipped'
    assert result.orders[0].order_purchase_timestamp==datetime(2017,1,1)


@pytest.mark.parametrize('identity',['unknown','c1',"repeat' OR 1=1 --"])
def test_unknown_identity_and_customer_id_not_used(connection,identity):
    add_order(connection)
    result=get_customer_order_history(connection,identity)
    assert not result.found
    assert result.customer_record_count==0
    assert result.order_count==0
    assert result.orders==[]
    assert result.customer_unique_id==identity


def test_known_customer_without_orders(connection):
    result=get_customer_order_history(connection,'repeat')
    assert result.found and result.customer_record_count==3 and result.order_count==0


@pytest.mark.parametrize('prices,payments,freight,price_total,payment_total,sellers',[
    (['10.00'],['12.00'],'2.00','10.00','12.00',1),
    (['10.00','20.00'],['34.00'],'2.00','30.00','34.00',2),
    (['10.00'],['4.00','8.00'],'2.00','10.00','12.00',1),
    (['10.00','20.00'],['12.00','22.00'],'2.00','30.00','34.00',2),
    (['0.10','0.20'],['0.30'],'0.00','0.30','0.30',2),
    (['10.00'],['0.00'],'0.00','10.00','0.00',1),
])
def test_financial_totals(connection,prices,payments,freight,price_total,payment_total,sellers):
    add_order(connection)
    add_children(connection,prices,payments,freight)
    result=get_order_financial_summary(connection,'order1')
    assert result.item_count==len(prices)
    assert result.seller_count==sellers
    assert result.payment_row_count==len(payments)
    assert result.items_price_total==Decimal(price_total)
    assert result.freight_total==Decimal(freight)*len(prices)
    assert result.items_and_freight_total==Decimal(price_total)+Decimal(freight)*len(prices)
    assert result.payment_total==Decimal(payment_total)
    assert result.difference==result.payment_total-result.items_and_freight_total
    assert isinstance(result.payment_total,Decimal)
    if len(prices)==2 and len(payments)==2:
        # A raw child join would produce four rows, item price 60 and payments 68.
        assert result.item_count==2 and result.payment_row_count==2
        assert result.items_price_total==Decimal('30.00')
        assert result.payment_total==Decimal('34.00')
        assert result.difference==Decimal('0.00')


@pytest.mark.parametrize('prices,payments', [([],['5.00']), (['5.00'],[]), ([],[])])
def test_missing_children_remain_missing(connection,prices,payments):
    add_order(connection)
    add_children(connection,prices,payments)
    result=get_order_financial_summary(connection,'order1')
    assert result.item_count==len(prices)
    assert result.payment_row_count==len(payments)
    if not prices:
        assert result.seller_count==0
        assert result.items_price_total is None
        assert result.freight_total is None
        assert result.items_and_freight_total is None
    if not payments:
        assert result.payment_total is None
    assert result.difference is None


@pytest.mark.parametrize('order_id',['unknown',"order1' OR 1=1 --"])
def test_unknown_order(connection,order_id):
    add_order(connection)
    assert get_order_financial_summary(connection,order_id) is None


@pytest.mark.parametrize('function',[get_customer_order_history,get_order_financial_summary])
def test_blank_identity_rejected(connection,function):
    with pytest.raises(ValueError,match='nonblank'):
        function(connection,'   ')


def test_all_functions_execute_only_selects(connection):
    add_order(connection)
    add_children(connection,['10.00'],['10.00'])
    statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany):
        statements.append(statement)
    event.listen(connection,'before_cursor_execute',capture)
    get_order_status_summary(connection)
    get_customer_order_history(connection,'repeat')
    get_order_financial_summary(connection,'order1')
    assert len(statements)==3
    assert all(sql.lstrip().upper().startswith('SELECT') for sql in statements)
    event.remove(connection,'before_cursor_execute',capture)


def test_financial_postgresql_query_uses_separate_aggregates_and_parameters():
    connection=MagicMock()
    connection.execute.return_value.mappings.return_value.one_or_none.return_value=None
    get_order_financial_summary(connection,"o' injected")
    statement=connection.execute.call_args.args[0]
    compiled=statement.compile(dialect=postgresql.dialect())
    sql=str(compiled)
    assert 'item_totals' in sql and 'payment_totals' in sql
    assert sql.count('GROUP BY')==2
    assert "o' injected" not in sql
    assert "o' injected" in compiled.params.values()
