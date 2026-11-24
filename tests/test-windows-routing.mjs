import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { isWindowsProject, selectBackend } from '../plugins/autodl-remote/windows/routing.mjs';

test('Linux/default preserves original binary and arguments', () => {
  const root = mkdtempSync(path.join(tmpdir(),'autodl-route-'));
  try { assert.deepEqual(selectBackend(['doctor'], root, '/original'), {executable:'/original',args:['doctor']}); }
  finally {rmSync(root,{recursive:true,force:true});}
});
test('project markers route transparently; nested Linux binding wins', () => {
  const root=mkdtempSync(path.join(tmpdir(),'autodl-route-'));
  try {
    writeFileSync(path.join(root,'.autodl-remote-windows.json'),'{}');
    const nested=path.join(root,'nested');mkdirSync(nested);
    assert.equal(isWindowsProject(nested),true);
    let route=selectBackend(['exec','--','python','x.py'],nested,'/original');
    assert.match(route.args[0],/controller\.py$/);assert.deepEqual(route.args.slice(1),['exec','--','python','x.py']);
    assert.equal(selectBackend(['account','list'],nested,'/original').executable,'/original');
    writeFileSync(path.join(nested,'.autodl-remote.conf'),'');assert.equal(isWindowsProject(nested),false);
    writeFileSync(path.join(nested,'.autodl-remote-windows.json'),'{}');assert.equal(isWindowsProject(nested),true);
  } finally {rmSync(root,{recursive:true,force:true});}
});
test('explicit Windows bind routes without project marker',()=>{
  const root=mkdtempSync(path.join(tmpdir(),'autodl-route-'));
  try { const route=selectBackend(['windows','bind'],root,'/original');assert.match(route.args[0],/controller\.py$/);assert.deepEqual(route.args.slice(1),['bind']); }
  finally {rmSync(root,{recursive:true,force:true});}
});
