import importlib
import unittest
D = importlib.import_module('dashboard.app')

class DataFabricDashboardTest(unittest.TestCase):
    def test_read_only_data_fabric_route(self):
        client = D.app.test_client()
        resp = client.get('/data-fabric')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn('providers', data)
        self.assertIn('snapshot', data)
        self.assertNotIn('execute', str(data).lower())

if __name__ == '__main__': unittest.main()
