"""Loopback form accepts embedded browsers but rejects CSRF and bad hosts."""
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
import launcher

class LauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),launcher.Handler)
        cls.base=f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def request(self,data,headers=None):
        req=urllib.request.Request(self.base+'/start',data=urllib.parse.urlencode(data).encode(),headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:return r.status,r.read().decode()
        except urllib.error.HTTPError as e:return e.code,e.read().decode()
    def test_embedded_browser_without_origin_reaches_validation(self):
        status,body=self.request({'nonce':launcher.NONCE,'token':'invalid'})
        self.assertEqual(status,400)
        self.assertIn('Нужен токен',body)
        self.assertIsNone(launcher.PROC)
    def test_cross_site_without_nonce_rejected(self):
        status,_=self.request({'token':'invalid'},{'Origin':'https://example.invalid'})
        self.assertEqual(status,403)
        self.assertIsNone(launcher.PROC)
    def test_wrong_nonce_rejected_even_with_same_origin(self):
        status,_=self.request({'nonce':'wrong','token':'invalid'},{'Origin':self.base})
        self.assertEqual(status,403)
    def test_wrong_host_rejected(self):
        status,_=self.request({'nonce':launcher.NONCE,'token':'invalid'},{'Host':'example.invalid'})
        self.assertEqual(status,403)

if __name__=='__main__':unittest.main()
