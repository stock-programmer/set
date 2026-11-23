/* Offline browser integration. The HTTP fixture accepts only local test messages. */
'use strict';
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const uuid = () => require('node:crypto').randomUUID().replaceAll('-','');
const {drive,pageFault,sourceRanges} = require('../../tools/workbench_loop_browser.cjs');

async function main() {
  const job = 'b'.repeat(32), project = 'c'.repeat(16), message = 'e'.repeat(32);
  const commission = 'f'.repeat(32);
  const writes = [];
  let injectFault = false;
  const runId=uuid(), attempt=uuid(), directory=path.resolve(__dirname,'../../outputs/workbench-loop',runId);
  const intentFile=path.join(directory,'intent-'+attempt+'.json');
  fs.mkdirSync(directory,{recursive:true});
  const html = `<!doctype html><html data-startup-ready-ms="10"><body>
    <form id="create-form"><textarea id="idea-input"></textarea><button type="submit">创作剧本并制作资产</button></form>
    <section id="agent-chat"><button aria-pressed="true">剧本 Agent</button><button aria-pressed="false">视频制作 Agent</button>
    <button id="resume">继续运行</button><textarea id="agent-chat-input"></textarea><button id="send">发送</button></section><section><button id="commission">继续当前制作委托</button></section>
    <dialog id="notice">有资产需要处理</dialog>
    <script>
      if(!window.pianchangFeedbackObserver) document.querySelector('#notice').showModal();
      const roleButtons=document.querySelectorAll('[aria-pressed]');
      roleButtons.forEach(b=>b.onclick=()=>roleButtons.forEach(x=>x.setAttribute('aria-pressed', String(x===b))));
      const post=(url,body)=>fetch(url,{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':'offline-only-key'},body:JSON.stringify(body)});
      document.querySelector('form').onsubmit=e=>{e.preventDefault();post('/api/studio/scripts',{idea:document.querySelector('textarea').value})};
      document.querySelector('#resume').onclick=()=>post('/api/pi/messages',{role:'script',text:'继续运行',scope:{project:'${project}'},message_id:'${message}'});
      document.querySelector('#send').onclick=()=>post('/api/pi/messages',{role:'script',text:document.querySelector('#agent-chat-input').value,scope:{project:'${project}'},message_id:'${message}'});
      document.querySelector('#commission').onclick=()=>post('/api/pi/messages',{role:'video',text:'继续当前制作委托',scope:{project:'${project}'},message_id:'${message}',continue_commission:'${commission}'});
      if(location.hash.includes('project='))setInterval(async()=>{
        if(await (await fetch('/listen-signal')).text()==='raise')setTimeout(()=>{throw new TypeError('private test exception')});
      },200);
    </script></body></html>`;
  const server=http.createServer(async(req,res)=>{
    if(req.method==='POST'){
      const chunks=[];for await(const chunk of req)chunks.push(chunk);
      writes.push({path:req.url,body:JSON.parse(Buffer.concat(chunks))});
      res.writeHead(200,{'Content-Type':'application/json'});
      res.end(JSON.stringify({job:{id:job}}));
    } else if(req.url==='/listen-signal'){
      res.end(injectFault?'raise':'idle');
    } else {
      res.writeHead(200,{'Content-Type':'text/html; charset=utf-8'});res.end(html);
    }
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const url='http://127.0.0.1:'+server.address().port;
  try {
    const prepared=await drive({action:'prepare',url});assert(prepared.can_submit);
    assert.equal(writes.length,0);
    const started=await drive({action:'start',url,idea:'fixture idea',run_id:runId,attempt});
    assert.equal(started.story_id,job,JSON.stringify(started));
    assert.deepEqual(JSON.parse(fs.readFileSync(intentFile,'utf8')),{
      action:'start',idempotency_key:'offline-only-key',message_id:null,commission_id:null,scope:{},role:'script'
    });
    const duplicate=await drive({action:'start',url,idea:'fixture idea',run_id:runId,attempt});
    assert.equal(duplicate.ok,false);assert.equal(duplicate.uncertain,true);assert.equal(writes.length,1);
    const observed=await drive({action:'observe',url,project_id:project,role:'video'});
    assert.deepEqual(observed.scope,{project});assert.equal(writes.length,1);
    const resumed=await drive({action:'continue',url,project_id:project,role:'script'});
    assert.equal(resumed.message_id,message,JSON.stringify(resumed));
    const correction=await drive({action:'continue',url,project_id:project,role:'script',repair_quantities:true});
    assert.equal(correction.ok,true);assert.match(writes.at(-1).body.text,/requested_quantity_checks/);
    const video=await drive({action:'continue',url,project_id:project,role:'video',commission_id:commission});
    assert.equal(video.commission_id,commission);assert.equal(video.job_id,job);
    assert.deepEqual(writes.map(r=>r.path),['/api/studio/scripts','/api/pi/messages','/api/pi/messages','/api/pi/messages']);
    const child=require('node:child_process').spawn(process.execPath,[path.join(__dirname,'../../tools/workbench_loop_browser.cjs'),'--listen'],{stdio:['pipe','pipe','inherit'],windowsHide:true});
    const exit=new Promise(resolve=>child.on('exit',resolve));
    const config={action:'listen',url,project_id:project,role:'script'};
    child.stdin.write(JSON.stringify(config)+'\n');
    const lines=require('node:readline').createInterface({input:child.stdout});
    const events=[];
    try {
      for await(const line of lines){
        const event=JSON.parse(line);events.push(event);
        if(event.event==='ready')injectFault=true;
      }
      assert.equal(await exit,1);
      assert.deepEqual(events.map(x=>x.event),['ready','fault']);
      assert.equal(events[1].fault.error_type,'TypeError');
      assert.equal(JSON.stringify(events).includes('private test exception'),false);
      assert.equal(writes.length,4);
    } finally { injectFault=false;child.stdin.end(); }
    const error={name:'TypeError',stack:'TypeError: SECRET\n    at fn ('+url+'/app.js:120:7)'};
    assert.deepEqual(pageFault(error,url),{code:'page_exception',error_type:'TypeError',frames:[{file:'web/app.js',line:120}]});
    const source=require('node:fs').readFileSync(require('node:path').join(__dirname,'../../web/agent-chat.js'),'utf8');
    for (const servedPath of ['/','/workbench.js']) {
      const maps = new Map([[servedPath,sourceRanges('one\ntwo\n'+source)]]);
      assert.deepEqual(pageFault({name:'TypeError',stack:url+servedPath+':9:2'},url,maps).frames,
        [{file:'web/agent-chat.js',line:7}]);
    }
    console.log('Passed browser submit, observation, script and frozen-commission continuation, and fault redaction.');
  } finally {
    server.closeAllConnections();await new Promise(resolve=>server.close(resolve));
    if(fs.existsSync(intentFile))fs.unlinkSync(intentFile);
    fs.rmdirSync(directory);
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
