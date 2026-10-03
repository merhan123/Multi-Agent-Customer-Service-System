"""Phase 2 contract tests with AWS clients and CLI deployment fully mocked."""
import unittest
from unittest.mock import Mock, patch
from botocore.validate import validate_parameters
from test_workflow_local import ao, config
import agentcore_cli


class DeploymentTests(unittest.TestCase):
    def test_observability_applies_logging_and_full_sampling(self):
        with patch.dict(config.__dict__, {'AGENT_LOG_GROUP':'/test/novamart'}), \
             patch.object(ao, 'apply_observability_config', return_value={'log_group':'/test/novamart'}) as apply:
            ao.configure_observability('runtime-arn')
        apply.assert_called_once_with('runtime-arn', {
            'cloudWatchConfig':{'logGroupName':'/test/novamart','logLevel':'INFO','enabled':True},
            'xRayConfig':{'enabled':True,'samplingRate':1.0}})

    def test_observability_failure_is_not_reported_as_success(self):
        with patch.dict(config.__dict__, {'AGENT_LOG_GROUP':'/test/novamart'}), \
             patch.object(ao, 'apply_observability_config', side_effect=RuntimeError('Access denied')):
            with self.assertRaisesRegex(RuntimeError, 'Access denied'):
                ao.configure_observability('runtime-arn')

    def test_guardrail_policies_and_numbered_version(self):
        client = Mock()
        client.list_guardrails.return_value = {'guardrails': []}
        client.create_guardrail.return_value = {'guardrailId': 'guardrail-test'}
        client.create_guardrail_version.return_value = {'version': '1'}
        # Validate request shape against the installed AWS SDK, without network calls.
        real_client = ao.boto3.client('bedrock', region_name='us-east-1')
        with patch.object(ao.boto3, 'client', return_value=client):
            self.assertEqual(ao.create_guardrail(), ('guardrail-test', '1'))
        request = client.create_guardrail.call_args.kwargs
        validate_parameters(request, real_client.meta.service_model.operation_model('CreateGuardrail').input_shape)
        strengths = {f['type']: (f['inputStrength'], f['outputStrength'])
                     for f in request['contentPolicyConfig']['filtersConfig']}
        self.assertEqual(strengths, {**dict.fromkeys(['SEXUAL','VIOLENCE','HATE'], ('HIGH','HIGH')),
                                    **dict.fromkeys(['INSULTS','MISCONDUCT'], ('MEDIUM','MEDIUM'))})
        self.assertEqual({p['type']: p['action'] for p in request['sensitiveInformationPolicyConfig']['piiEntitiesConfig']},
                         {'CREDIT_DEBIT_CARD_NUMBER':'BLOCK', 'US_SOCIAL_SECURITY_NUMBER':'BLOCK',
                          'EMAIL':'ANONYMIZE', 'PHONE':'ANONYMIZE'})
        self.assertEqual(request['topicPolicyConfig']['tierConfig'], {'tierName':'STANDARD'})
        self.assertEqual(request['crossRegionConfig'], {'guardrailProfileIdentifier':'us.guardrail.v1:0'})
        self.assertEqual(len(request['topicPolicyConfig']['topicsConfig']), 3)
        self.assertTrue(all(t['type'] == 'DENY' for t in request['topicPolicyConfig']['topicsConfig']))
        self.assertIn('Excludes arithmetic', request['topicPolicyConfig']['topicsConfig'][1]['definition'])
        self.assertEqual(request['wordPolicyConfig']['managedWordListsConfig'], [{'type':'PROFANITY'}])
        self.assertTrue(request['blockedInputMessaging'] and request['blockedOutputsMessaging'])
        client.create_guardrail_version.assert_called_once_with(guardrailIdentifier='guardrail-test')

    def test_existing_draft_is_published(self):
        client = Mock()
        client.list_guardrails.side_effect = [
            {'guardrails':[{'name':config.GUARDRAIL_NAME, 'id':'existing'}]},
            {'guardrails':[{'version':'DRAFT'}]}]
        client.create_guardrail_version.return_value = {'version':'2'}
        with patch.object(ao.boto3, 'client', return_value=client):
            self.assertEqual(ao.create_guardrail(), ('existing', '2'))
        client.create_guardrail.assert_not_called()

    def test_runtime_cli_contract(self):
        arn = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test'
        values = dict(AWS_REGION='us-east-1', PROJECT_NAME='novamart', RETURNS_KB_ID='returns',
                      SHIPPING_KB_ID='shipping', WARRANTY_KB_ID='warranty', AGENT_LOG_GROUP='/test',
                      ACCOUNT_ID='123456789012', AGENTCORE_ROLE_ARN='arn:aws:iam::123456789012:role/test')
        with patch.dict(config.__dict__, values), patch.object(agentcore_cli, 'stage_runtime_code') as stage, \
             patch.object(agentcore_cli, 'configure_runtime') as configure, \
             patch.object(agentcore_cli, 'deploy') as deploy, \
             patch.object(agentcore_cli, 'deployed_runtime_arn', side_effect=['', arn]), \
             patch.object(ao, 'wait_for_runtime_ready') as wait:
            self.assertEqual(ao.deploy_to_agentcore_runtime(Mock(), 'guardrail-test', '1'), arn)
        stage.assert_called_once_with()
        deploy.assert_called_once_with()
        configure.assert_called_once_with(
            env_vars={**{k:values[k] for k in ('AWS_REGION','PROJECT_NAME','RETURNS_KB_ID','SHIPPING_KB_ID','WARRANTY_KB_ID','AGENT_LOG_GROUP')},
                      'GUARDRAIL_ID':'guardrail-test','GUARDRAIL_VERSION':'1'},
            network_mode='PUBLIC', protocol='HTTP', execution_role_arn=values['AGENTCORE_ROLE_ARN'])
        wait.assert_called_once_with(ao.agentcore_control, 'test')

    def test_memory_strategy_retention_and_active_wait(self):
        client = Mock()
        client.list_memories.return_value = {'memories':[]}
        client.create_memory.return_value = {'memory':{'id':'memory-test','arn':'memory-arn','status':'CREATING'}}
        client.get_memory.return_value = {'memory':{'id':'memory-test','arn':'memory-arn','status':'ACTIVE'}}
        with patch.object(ao, 'agentcore_control', client), patch.object(ao.time, 'sleep'):
            self.assertEqual(ao.configure_memory('runtime-arn'), 'memory-arn')
        request = client.create_memory.call_args.kwargs
        validate_parameters(request, ao.agentcore_control.meta.service_model.operation_model('CreateMemory').input_shape)
        self.assertEqual(request['eventExpiryDuration'], 7)
        self.assertEqual(request['memoryStrategies'], [{'summaryMemoryStrategy':{
            'name':'SessionSummary', 'namespaces':['/summaries/{actorId}/{sessionId}']}}])
        self.assertTrue(request['clientToken'])
        client.get_memory.assert_called_once_with(memoryId='memory-test')


if __name__ == '__main__':
    unittest.main()
