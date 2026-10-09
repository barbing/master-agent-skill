from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

from master_agent_runtime.backend import select_cli
from master_agent_runtime.native import ModelUnavailable


class BackendSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_compatible_cli_selected_without_substituting_global_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            old=Path(tmp)/'old';new=Path(tmp)/'new';old.touch();new.touch()
            clients=[]
            def factory(argv):
                client=AsyncMock();clients.append(client)
                async def call(method,params):
                    if method=='config/read':return {'config':{'model':'requested'}}
                    return {'data':[{'model':'requested' if argv[0]==str(new) else 'other'}]}
                client.call.side_effect=call;return client
            with patch('master_agent_runtime.backend.sandbox_prefix'),patch('master_agent_runtime.backend.JsonRpcClient',side_effect=factory):
                result=await select_cli(candidates=(str(old),str(new)))
            self.assertEqual(result,str(new))
            self.assertTrue(all(client.close.await_count==1 for client in clients))
            self.assertTrue(all(c.args[0] not in {'thread/start','turn/start'} for client in clients for c in client.call.call_args_list))

    async def test_unavailable_requested_model_has_no_silent_catalog_default_fallback(self):
        with tempfile.NamedTemporaryFile() as existing:
            client=AsyncMock()
            async def call(method,params):
                if method=='config/read':return {'config':{'model':'requested'}}
                return {'data':[{'model':'different-default','isDefault':True}]}
            client.call.side_effect=call
            with patch('master_agent_runtime.backend.sandbox_prefix'),patch('master_agent_runtime.backend.JsonRpcClient',return_value=client):
                with self.assertRaises(ModelUnavailable):await select_cli(candidates=(existing.name,))
