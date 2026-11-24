import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

BASE=Path(__file__).resolve().parents[1]/'plugins/autodl-remote/windows'
sys.path.insert(0,str(BASE))
import common
import controller
import worker

class CommonTests(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(common.windows_root(r'd:\GPU Projects\demo'), 'D:/GPU Projects/demo')
        self.assertEqual(common.relative(r'src\train.py'),'src/train.py')
        for path in ('D:/','D:foo','/root','D:/a/../b','D:/NUL.txt'):
            with self.subTest(path=path),self.assertRaises(ValueError):common.windows_root(path)
        for path in ('../a',r'a\..\b','C:/abc',r'C:abc',r'\\host\share','/tmp','a:ads','foo.','foo ','NUL','COM1.txt','.autodl-remote/secret','.git/config'):
            with self.subTest(path=path),self.assertRaises(ValueError):common.relative(path)
    def test_unicode_and_argv(self):
        req={'argv':['python','train.py','a b','',"quote'\"",'测试','; rm -rf /']}
        self.assertEqual(common.decode(common.encode(req)), req)
        self.assertEqual(common.ps_quote("O'Brien"), "'O''Brien'")
        self.assertNotIn('测试',common.ps_command("'测试'"))
    def test_names(self):
        for name in ('../bad','..','CON','x.','space name','a/b'):
            with self.subTest(name=name),self.assertRaises(ValueError):common.job_name(name)
        self.assertEqual(common.job_name('train-seed_01'),'train-seed_01')
    def test_sftp_path(self):
        self.assertEqual(controller.sftp_remote('D:/GPU Projects/demo','src/x.py'),'/D:/GPU Projects/demo/src/x.py')
        self.assertEqual(controller.batch_quote('D:/GPU Projects/demo'), '"D:/GPU Projects/demo"')
        for path in ('a\nb','a"b','a[b]'):
            with self.assertRaises(ValueError):controller.batch_quote(path)
    def test_host(self):
        self.assertEqual(controller.ssh_target('user@192.168.4.65'),'user@192.168.4.65')
        for target in ('-oProxyCommand=foo','user@host;foo','user@host\n'):
            with self.assertRaises(ValueError):controller.ssh_target(target)
    def test_exec_argparse(self):
        a=controller.parser().parse_args(['exec','--detach','--name','train','--','python','train.py','a b','--lr','0.1'])
        self.assertTrue(a.detach);self.assertEqual(a.argv[1:],['python','train.py','a b','--lr','0.1'])
        a=controller.parser().parse_args(['run','submit','--sync-up','src','--name','r1','--','python','src/train.py'])
        self.assertEqual(a.sync_up,['src']);self.assertEqual(a.argv[1:],['python','src/train.py'])
        a=controller.parser().parse_args(['put','--','src/a.py','remote/a.py']);self.assertEqual(a.paths,['src/a.py','remote/a.py'])
        a=controller.parser().parse_args(['tail','--lines','12','--','output.log']);self.assertEqual(a.path,'output.log')
        a=controller.parser().parse_args(['run','note','train','--tag','seed=1']);self.assertEqual(a.tag,['seed=1'])

class FilesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve();self.addCleanup(self.temp.cleanup)
        (self.root/'src').mkdir();(self.root/'src'/'a.py').write_text('hello\nworld',encoding='utf-8')
    def test_marker_nearest(self):
        marker=self.root/controller.MARKER;marker.write_text('{}')
        self.assertEqual(controller.find_binding(self.root/'src'),marker)
        (self.root/'src'/'.autodl-remote.conf').write_text('')
        self.assertIsNone(controller.find_binding(self.root/'src'))
        (self.root/'.autodl-remote.conf').write_text('')
        with self.assertRaises(ValueError):controller.find_binding(self.root)
    def test_local_symlink(self):
        link=self.root/'link';link.symlink_to(self.root/'src',target_is_directory=True)
        with self.assertRaises(ValueError):controller.absolute_local(self.root,'link/a.py',must_exist=True)
        with self.assertRaises(ValueError):worker.under(self.root,'link/a.py')
    def test_file_manifest_and_limit(self):
        rows=worker.manifest(self.root,self.root/'src',100,5000,True)
        self.assertEqual([r['path'] for r in rows],['src','src/a.py'])
        with self.assertRaises(ValueError):worker.manifest(self.root,self.root/'src',0,5000,True)
        with self.assertRaises(ValueError):worker.manifest(self.root,self.root/'src',100,1,True)
    def test_bounded_logs(self):
        out=io.StringIO()
        with contextlib.redirect_stdout(out):worker.bounded_text(self.root/'src'/'a.py',1,True)
        self.assertEqual(out.getvalue(),'world\n')
    def test_download_collision(self):
        c=controller.Client.__new__(controller.Client);c.project=self.root;c.root='D:/demo'
        c.request_json=Mock(return_value=[{'path':'src/a.py','type':'file','size':1}]);c.sftp=Mock()
        with self.assertRaises(ValueError):c.download('src/a.py')
        c.sftp.assert_not_called()
    def test_upload_dry_run(self):
        c=controller.Client.__new__(controller.Client);c.project=self.root;c.root='D:/demo';c.request=Mock();c.sftp=Mock()
        with contextlib.redirect_stdout(io.StringIO()) as out:c.upload('src',dry=True)
        self.assertEqual(len(json.loads(out.getvalue())),2);c.request.assert_not_called();c.sftp.assert_not_called()
    def test_remote_exec_preserves_argv(self):
        cmd=['python','src/a.py','x y','中文','"quote"','']
        with patch.object(worker.subprocess,'call',return_value=7) as run:
            rc=worker.dispatch({'op':'exec','root':str(self.root),'argv':cmd,'cwd':'.'})
        self.assertEqual(rc,7);self.assertEqual(run.call_args.args[0],[sys.executable,*cmd[1:]])
        self.assertFalse(run.call_args.kwargs['shell'])
    def test_unsupported_executable(self):
        with self.assertRaises(ValueError):worker.resolve_argv(['powershell.exe','-Command','...'])
    def test_job_submit_and_duplicate(self):
        req={'name':'test-job','cwd':'src','argv':['python','a.py'],'metadata':{'model':'demo'}}
        with patch.object(worker,'ps',return_value='') as ps, contextlib.redirect_stdout(io.StringIO()) as out:
            worker.submit(self.root,req)
            data=json.loads(out.getvalue());self.assertEqual(data['status'],'submitted')
            script=ps.call_args.args[0];self.assertIn('S4U',script);self.assertIn('RunLevel Limited',script);self.assertIn('Register-ScheduledTask',script)
            with self.assertRaises(FileExistsError):worker.submit(self.root,req)
        spec=worker.read_json(worker.job_directory(self.root,'test-job')/'spec.json')
        self.assertEqual(spec['argv'],[sys.executable,'a.py'])
    def test_job_exit_code(self):
        with patch.object(worker,'ps',return_value=''),contextlib.redirect_stdout(io.StringIO()):
            worker.submit(self.root,{'name':'test-job','argv':['python','src/a.py']})
        d=worker.job_directory(self.root,'test-job')
        with patch.object(worker,'assign_kill_on_close_job',return_value=123),patch.object(worker.subprocess,'call',return_value=9):
            self.assertEqual(worker.run_job(d/'spec.json'),9)
        state=worker.job_state(d);self.assertEqual(state['exit_code'],9);self.assertEqual(state['status'],'failed')
    def test_job_isolation_failure(self):
        with patch.object(worker,'ps',return_value=''),contextlib.redirect_stdout(io.StringIO()):
            worker.submit(self.root,{'name':'test-job','argv':['python','src/a.py']})
        d=worker.job_directory(self.root,'test-job')
        with patch.object(worker,'assign_kill_on_close_job',side_effect=OSError('assign denied')),patch.object(worker.subprocess,'call') as run:
            self.assertEqual(worker.run_job(d/'spec.json'),1);run.assert_not_called()
        self.assertEqual(worker.job_state(d)['status'],'failed')
    def test_job_stop_does_not_cancel_completed(self):
        with patch.object(worker,'ps',return_value=''),contextlib.redirect_stdout(io.StringIO()):
            worker.submit(self.root,{'name':'test-job','argv':['python','src/a.py']})
        d=worker.job_directory(self.root,'test-job')
        worker.atomic_json(d/'state.json',{'status':'exited','exit_code':0})
        with patch.object(worker,'ps') as ps,contextlib.redirect_stdout(io.StringIO()):worker.stop_job(self.root,'test-job');ps.assert_not_called()
        self.assertEqual(worker.job_state(d)['status'],'exited')
    def test_job_stop_checks_scheduler(self):
        with patch.object(worker,'ps',return_value=''),contextlib.redirect_stdout(io.StringIO()):
            worker.submit(self.root,{'name':'test-job','argv':['python','src/a.py']})
        d=worker.job_directory(self.root,'test-job');worker.atomic_json(d/'state.json',{'status':'running'})
        with patch.object(worker,'task_status',side_effect=['Running','Ready']),patch.object(worker,'ps',side_effect=['','stopped']) as ps,contextlib.redirect_stdout(io.StringIO()):
            worker.stop_job(self.root,'test-job');self.assertIn('Stop-ScheduledTask',ps.call_args_list[0].args[0])
        self.assertEqual(worker.job_state(d)['status'],'cancelled')

if __name__=='__main__':unittest.main(verbosity=2)
