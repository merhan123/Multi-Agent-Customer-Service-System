"""Offline behavioral checks; no cloud resources or model invocations."""
import os
os.environ['AWS_ACCESS_KEY_ID'] = 'testing'
os.environ['AWS_SECRET_ACCESS_KEY'] = 'testing'
os.environ['AWS_EC2_METADATA_DISABLED'] = 'true'
os.environ['AGENT_TRACING_ENABLED'] = 'false'
import sys
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parents[1])]
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone, timedelta
from threading import Barrier
from types import SimpleNamespace
from botocore.exceptions import ClientError
import config
config.GUARDRAIL_ID = ''
config.GUARDRAIL_VERSION = ''
config.ORDERS_TABLE = 'orders'
config.CUSTOMERS_TABLE = 'customers'
config.WORKFLOW_STATE_TABLE = 'state'
config.RETURNS_KB_ID = 'returns'
config.SHIPPING_KB_ID = 'shipping'
config.WARRANTY_KB_ID = 'warranty'
import agent_orchestrator as ao

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(ao, 'tool', lambda f: f), patch.object(ao, 'BedrockModel'),
                        patch.object(ao, 'Agent', side_effect=lambda **kw: SimpleNamespace(**kw)),
                        patch.object(ao, 'trace')]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_discount_rounds_only_final_total(self):
        calculator = ao.build_communication_agent().tools[1]
        self.assertEqual(calculator(5, '29.99', '10')['final_total'], '134.96')
        self.assertEqual(calculator(5, '29.99', '10')['discount_amount'], '14.995')
        self.assertIn('error', calculator(1, 'NaN', '10'))
        self.assertIn('error', calculator(0, '10', '20'))

    def refund(self, tier, days, status='delivered', reference=None):
        order = {'order_date': (datetime.now(timezone.utc).date()-timedelta(days=days)).isoformat(),
                 'status': status, 'price': '89.99', 'quantity': '2'}
        if reference:
            order['return_reference'] = reference
        orders, customers = Mock(), Mock()
        orders.get_item.return_value = {'Item': order}
        customers.get_item.return_value = {'Item': {'tier': tier}}
        with patch.object(ao, 'dynamodb') as db:
            db.Table.side_effect = lambda name: orders if name == 'orders' else customers
            tool = ao.build_refund_agent().tools[1]
            result = tool('CUST-001', 'ORD-001', 'Does not fit')
        return result, orders

    def test_refund_windows_and_quantity(self):
        for tier, days, approved in [('Standard',30,True),('Standard',31,False),
                                     ('Premium',60,True),('Premium',61,False)]:
            with self.subTest(tier=tier,days=days):
                result, orders = self.refund(tier,days)
                self.assertEqual(result['status'],'approved' if approved else 'denied')
                self.assertEqual(orders.update_item.call_count,int(approved))
                if approved:
                    self.assertEqual(result['amount'],'179.98')
                    self.assertIn('attribute_not_exists', orders.update_item.call_args.kwargs['ConditionExpression'])

    def test_repeat_refund_does_not_write(self):
        result, orders = self.refund('Premium',10,'return_requested','RET-existing')
        self.assertEqual(result['return_reference'],'RET-existing')
        orders.update_item.assert_not_called()

    def test_undelivered_and_future_orders_denied(self):
        for days, status in [(1,'shipped'),(-1,'delivered')]:
            result, orders=self.refund('Premium',days,status)
            self.assertEqual(result['status'],'denied')
            orders.update_item.assert_not_called()

    def test_inventory_paginates_and_uses_composite_key(self):
        table=Mock()
        table.query.side_effect=[{'Items':[{'order_id':'1'}], 'LastEvaluatedKey':{'order_id':'1'}},
                                 {'Items':[{'order_id':'2'}]}]
        table.get_item.return_value={'Item':{'order_id':'1'}}
        with patch.object(ao,'dynamodb') as db:
            db.Table.return_value=table
            agent=ao.build_inventory_agent()
            self.assertEqual(len(agent.tools[2]('CUST-001')['orders']),2)
            agent.tools[0]('CUST-001','1')
        self.assertEqual(table.get_item.call_args.kwargs['Key'],{'customer_id':'CUST-001','order_id':'1'})

    def test_conflict_retries_with_current_version(self):
        class Conflict(Exception): pass
        db=Mock()
        db.meta.client.exceptions.ConditionalCheckFailedException=Conflict
        table=db.Table.return_value
        table.update_item.side_effect=[Conflict(),{}]
        with patch.object(ao,'dynamodb',db), patch.object(ao,'_read_workflow_state',side_effect=[{'version':2},{'version':3}]), patch.object(ao.time,'sleep'):
            result=ao._update_workflow_state('session',{'inventory_agent':'facts'},1)
        self.assertEqual(result['version'],3)
        self.assertEqual(table.update_item.call_args.kwargs['ExpressionAttributeValues'][':expected_version'],2)
        self.assertEqual(table.update_item.call_args.kwargs['ConditionExpression'],'version = :expected_version')

    def test_wrong_customer_and_missing_inventory_block_routing(self):
        worker=Mock()
        agent=ao.build_orchestrator_agent(worker,worker,worker,worker)
        tools={t.__name__:t for t in agent.tools}
        with patch.object(ao,'_read_workflow_state',return_value={'customer_id':'CUST-001','version':0}):
            with self.assertRaises(ValueError): tools['initialize_session']('s','CUST-002')
            with self.assertRaises(ValueError): tools['route_to_inventory_agent']('s','CUST-002','request')
            with self.assertRaises(ValueError): tools['route_to_refund_agent']('s','CUST-001','request')
        worker.assert_not_called()

    def test_three_retrievers_actually_run_concurrently(self):
        barrier=Barrier(3)
        def factory(**kw):
            if 'Retriever' in kw['name']:
                def run(query):
                    barrier.wait(timeout=3)
                    return kw['name']+' evidence'
                run.messages = []
                return run
            return SimpleNamespace(**kw)
        with patch.object(ao,'Agent',side_effect=factory):
            agent=ao.build_policy_agent()
            result=agent.tools[0]('return and warranty question')
        for domain in ('Returns','Shipping','Warranty'):
            self.assertIn(domain+'PolicyRetrieverAgent evidence',result)

if __name__=='__main__': unittest.main()
