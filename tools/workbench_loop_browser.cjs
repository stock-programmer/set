/* Internal browser adapter for the traced workbench loop. One JSON command per process. */
'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {createRequire} = require('node:module');

const storyId = /^[a-f0-9]{32}$/;
const projectId = /^[a-f0-9]{16}$/;
const root = path.resolve(__dirname, '..');

function chromium() {
  const bundled = path.join(os.homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/package.json');
  for (const resolve of [require, createRequire(bundled)]) {
    for (const name of ['playwright', 'playwright-core']) {
      try { return resolve(name).chromium; } catch (error) {
        if (error.code !== 'MODULE_NOT_FOUND') throw error;
      }
    }
  }
  throw new Error('browser_dependency_missing');
}

function checkedCommand(input) {
  const origin = new URL(input.url);
  if (origin.protocol !== 'http:' || !['localhost', '127.0.0.1', '[::1]'].includes(origin.hostname)
      || origin.username || origin.password || origin.href !== origin.origin + '/') throw new Error('invalid_origin');
  if (!['prepare','observe','start','continue','listen'].includes(input.action)) throw new Error('invalid_action');
  if ((input.run_id !== undefined || input.attempt !== undefined) &&
      (!storyId.test(input.run_id || '') || !storyId.test(input.attempt || ''))) throw new Error('invalid_intent');
  const role = input.role || 'script';
  if (!['script','video'].includes(role)) throw new Error('invalid_role');
  if (input.story_id && !storyId.test(input.story_id)) throw new Error('invalid_story');
  if (input.project_id && !projectId.test(input.project_id)) throw new Error('invalid_project');
  if (input.commission_id && !storyId.test(input.commission_id)) throw new Error('invalid_commission');
  const newStory = ['prepare','start'].includes(input.action);
  if (newStory && (input.story_id || input.project_id || role !== 'script')) throw new Error('invalid_scope');
  if (!newStory && !input.story_id && !input.project_id) throw new Error('missing_scope');
  if (input.action === 'start' && (typeof input.idea !== 'string' || !input.idea.trim() || input.idea.length > 6000)) throw new Error('invalid_idea');
  return {...input, role, origin:origin.origin, newStory};
}

function sourceRanges(body) {
  body = body.replace(/\r\n?/g,'\n');
  const ranges = [];
  for (const name of fs.readdirSync(path.join(root,'web')).filter(name => /^[\w-]+\.js$/.test(name))) {
    const file = 'web/' + name;
    const source = fs.readFileSync(path.join(root,file),'utf8').replace(/\r\n?/g,'\n');
    const index = body.indexOf(source);
    if (index === -1) continue;
    const start = body.slice(0,index).split('\n').length;
    ranges.push({file,start,end:start + source.trimEnd().split('\n').length - 1});
  }
  return ranges;
}

function pageFault(error, origin, maps = new Map()) {
  const frames = [];
  for (const match of String(error.stack || '').matchAll(/(https?:\/\/[^\s()]+):(\d+):(\d+)/g)) {
    if (!URL.canParse(match[1])) continue;
    const url = new URL(match[1]);
    if (url.origin !== origin) continue;
    const line = Number(match[2]), range = maps.get(url.pathname)?.find(r=>line>=r.start && line<=r.end);
    if (range) frames.push({file:range.file,line:line-range.start+1});
    else if (/^\/[\w-]+\.js$/.test(url.pathname) && fs.existsSync(path.join(root,'web',url.pathname.slice(1))))
      frames.push({file:'web'+url.pathname,line});
    if (frames.length >= 4) break;
  }
  return {code:'page_exception', error_type:['TypeError','ReferenceError','RangeError','SyntaxError'].includes(error.name) ? error.name : 'Error', frames};
}

async function launch() {
  const engine = chromium();
  // Playwright's bundled headless shell avoids spinning up a desktop Chrome profile.
  try { return await engine.launch({headless:true,timeout:25000}); } catch {}
  for (const channel of ['chrome','msedge']) {
    try { return await engine.launch({headless:true,channel,timeout:25000}); } catch {}
  }
  throw new Error('browser_executable_missing');
}

async function drive(input, listener) {
  const c = checkedCommand(input);
  if (c.action === 'listen' && !listener) throw new Error('listen_channel_required');
  const result = {ok:false,action:c.action,role:c.role,uncertain:false};
  const quantityRepair = c.action === 'continue' && c.role === 'script' && c.repair_quantities === true;
  const scriptMessage = quantityRepair
    ? '继续运行。请读取原创意和当前分镜，按 check_production_spec 的 requested_quantity_checks 修正镜头数量与时长。保留已发布剧本和所有必拍节拍，通过校验后发布修订的制作规格。'
    : '继续运行';
  const browser = await launch();
  let captureFault = async () => {};
  try {
    const context = await browser.newContext({serviceWorkers:'block'});
    const page = await context.newPage();
    // The watcher reads the same activity card without a human present to dismiss its notification.
    await page.addInitScript(() => { window.pianchangFeedbackObserver = true; });
    page.setDefaultTimeout(90000);
    const timeout = 90000;
    const expectedPath = c.action === 'start' ? '/api/studio/scripts' : '/api/pi/messages';
    let armed = false, allowedProject = c.project_id || null, pendingRequest, pageError;
    const maps = new Map(), mapReads = [];
    let resolveReply;
    const reply = new Promise(resolve => { resolveReply = resolve; });
    let signalFault;
    const faultRaised = new Promise(resolve => {signalFault=resolve;});
    page.on('pageerror', error => { pageError ||= error; signalFault('page_exception'); });
    page.on('crash', () => signalFault('browser_page_crashed'));
    page.on('close', () => signalFault('browser_page_closed'));
    page.on('response', response => {
      const url = new URL(response.url());
      if (url.origin === c.origin && ['/','/index.html','/workbench.js'].includes(url.pathname)) {
        mapReads.push(response.text().then(body=>maps.set(url.pathname,sourceRanges(body))).catch(()=>{}));
      }
    });
    captureFault = async () => {
      if (!pageError) return;
      await Promise.all(mapReads);
      result.fault = pageFault(pageError,c.origin,maps);
    };
    await context.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url());
      if (url.origin !== c.origin) return route.abort();
      if (['GET','HEAD'].includes(req.method())) return route.continue();
      if (!armed || pendingRequest || req.method() !== 'POST' || url.pathname !== expectedPath) return route.abort();
      let body;
      try { body = req.postDataJSON(); } catch { return route.abort(); }
      if (c.action === 'start') {
        if (body?.idea !== c.idea.trim() || Object.keys(body).some(k => k !== 'idea')) return route.abort();
      } else {
        const scope = body?.scope;
        const scopeOK = scope && Object.keys(scope).length === 1 &&
          ((c.story_id && scope.story === c.story_id) || (allowedProject && scope.project === allowedProject));
        if (!scopeOK || body.role !== c.role || !storyId.test(body.message_id)) return route.abort();
        if (c.role === 'video' ? !storyId.test(body.continue_commission || '') || (c.commission_id && body.continue_commission !== c.commission_id)
            : body.text !== scriptMessage || body.continue_commission) return route.abort();
        result.message_id = body.message_id;
        if (body.continue_commission) result.commission_id = body.continue_commission;
      }
      if (c.run_id) {
        const key = req.headers()['idempotency-key'];
        if (typeof key !== 'string' || !/^[a-zA-Z0-9-]{1,100}$/.test(key)) {
          resolveReply(null); return route.abort();
        }
        let fd;
        try {
          const filename = path.join(root,'outputs','workbench-loop',c.run_id,'intent-'+c.attempt+'.json');
          fd = fs.openSync(filename,'wx',0o600);
          fs.writeFileSync(fd,JSON.stringify({action:c.action,idempotency_key:key,message_id:result.message_id||null,
            commission_id:result.commission_id||null,scope:body.scope||{},role:c.role}));
          fs.fsyncSync(fd);
        } catch (error) {
          if (error.code === 'EEXIST') result.uncertain = true;
          resolveReply(null); return route.abort();
        } finally { if(fd !== undefined) fs.closeSync(fd); }
      }
      pendingRequest = req; result.uncertain = true;
      await route.continue();
    });
    page.on('requestfailed', req => { if (req === pendingRequest) resolveReply(null); });
    page.on('response', response => { if (response.request() === pendingRequest) resolveReply(response); });
    const route = new URLSearchParams();
    if (c.newStory) route.set('step','new');
    else if (!c.story_id) {
      route.set('project',c.project_id);
      route.set('step',c.role === 'video' ? 'video' : 'story');
    } else route.set('story',c.story_id);
    // The workbench itself links a completed draft to its production project.
    const draftReply = c.story_id ? page.waitForResponse(r =>
      r.url() === c.origin + '/api/story-drafts/' + c.story_id && r.request().method() === 'GET',
      {timeout}).then(r => r.ok() ? r.json() : null).catch(() => null) : null;
    await page.goto(c.origin + '/#' + route, {waitUntil:'domcontentloaded', timeout});
    if (draftReply) {
      const draft = await draftReply;
      if (!draft || (draft.id && draft.id !== c.story_id)) throw new Error('story_load_failed');
      if (c.project_id && c.project_id !== draft.project_id) throw new Error('scope_mismatch');
      if (projectId.test(draft.project_id)) allowedProject = draft.project_id;
    }
    await page.locator('html[data-startup-ready-ms]').waitFor({state:'attached',timeout});
    let submit;
    if (c.newStory) {
      await page.locator('#idea-input').waitFor({state:'visible'});
      submit = page.locator('#create-form button[type=submit]').first();
      if (c.action === 'start') await page.locator('#idea-input').fill(c.idea.trim());
    } else {
      let active = new URLSearchParams(new URL(page.url()).hash.slice(1));
      const sameStory = c.story_id && active.get('story') === c.story_id;
      const sameProject = allowedProject && active.get('project') === allowedProject;
      if (!sameStory && !sameProject) throw new Error('scope_mismatch');
      if (c.role === 'video' && sameProject && active.get('step') !== 'video') {
        await page.locator('[data-step="video"]').click();
        await page.waitForURL(url => new URLSearchParams(url.hash.slice(1)).get('step') === 'video');
      }
      const chat = page.locator('#agent-chat');
      const tab = chat.getByRole('button',{name:c.role === 'script' ? '剧本 Agent' : '视频制作 Agent',exact:true});
      if (await tab.getAttribute('aria-pressed') !== 'true') await tab.click();
      await chat.locator('[aria-pressed="true"]').filter({hasText:c.role === 'script' ? '剧本 Agent' : '视频制作 Agent'}).waitFor();
      active = new URLSearchParams(new URL(page.url()).hash.slice(1));
      result.scope = active.has('project') ? {project:active.get('project')} : {story:active.get('story')};
      if (result.scope.project ? result.scope.project !== allowedProject : result.scope.story !== c.story_id) throw new Error('scope_mismatch');
      if (c.story_id) result.story_id = c.story_id;
      if (result.scope.project) result.project_id = result.scope.project;
      submit = c.role === 'video' ? page.getByRole('button',{name:'继续当前制作委托',exact:true})
        : chat.getByRole('button',{name:'继续运行',exact:true});
      if (quantityRepair) {
        await chat.locator('#agent-chat-input').fill(scriptMessage);
        submit=chat.getByRole('button',{name:'发送',exact:true});
      }
    }
    result.can_submit = (await submit.count()) === 1 && await submit.isEnabled();
    await captureFault();
    if (result.fault) throw new Error('page_exception');
    if (c.action === 'prepare' || c.action === 'observe') return {...result,ok:true};
    if (c.action === 'listen') {
      listener.ready({...result,ok:true,event:'ready'});
      const stopped = await Promise.race([listener.stopped.then(()=> 'closed'),faultRaised]);
      await captureFault();
      if (stopped !== 'closed' || result.fault) throw new Error(stopped);
      return {...result,ok:true};
    }
    if (!result.can_submit) throw new Error(c.role === 'video' ? 'authorization_or_resumable_commission_required' : 'ui_submit_disabled');
    armed = true;
    await submit.click();
    let timer;
    let response;
    try { response = await Promise.race([reply,new Promise(resolve => { timer=setTimeout(() => resolve(null), timeout); })]); }
    finally { clearTimeout(timer); }
    if (!response) throw new Error('submission_receipt_missing');
    result.http_status = response.status();
    if (!response.ok()) throw new Error('submission_http_error');
    const data = await response.json();
    // A queued turn may return another turn's running job. Only its own
    // persisted message record can identify that turn's eventual execution.
    const id = data.queued ? data.message?.job_id : (data.message?.job_id || data.job?.id);
    if (storyId.test(data.session_id || '')) result.session_id = data.session_id;
    if (data.queued && result.message_id && data.message?.id === result.message_id) {
      return {...result,ok:true,queued:true,uncertain:false};
    }
    if (!storyId.test(id || '')) throw new Error('submission_job_unknown');
    result.job_id = id;
    if (c.action === 'start') result.story_id = id;
    if (storyId.test(data.job?.trace_id || '')) result.trace_id = data.job.trace_id;
    await captureFault();
    if (result.fault) throw new Error('page_exception');
    return {...result,ok:true,uncertain:false};
  } catch (error) {
    await captureFault();
    const code = result.fault ? 'page_exception' : /^[a-z_]+$/.test(error.message) ? error.message :
      error.name === 'TimeoutError' ? 'ui_timeout' : 'browser_action_failed';
    return {...result,error:code};
  } finally { await browser.close(); }
}

if (require.main === module) {
  (async () => {
    const write = data => process.stdout.write(JSON.stringify(data)+'\n');
    const listen = process.argv[2] === '--listen';
    let result;
    if (listen) {
      const lines = require('node:readline').createInterface({input:process.stdin,terminal:false});
      const stopped = new Promise(resolve=>lines.once('close',resolve));
      const first = new Promise(resolve=>lines.once('line',resolve));
      try {
        const line = await Promise.race([first,stopped.then(()=>null)]);
        if (!line) throw new Error('listen_config_required');
        const input = JSON.parse(line.replace(/^\uFEFF/,''));
        if (input.action !== 'listen') throw new Error('invalid_action');
        result = await drive(input,{ready:write,stopped});
      } finally { lines.close(); process.stdin.pause(); }
      write({...result,event:result.ok ? 'closed' : 'fault'});
    } else {
      const input = JSON.parse(fs.readFileSync(0,'utf8').replace(/^\uFEFF/,''));
      result = await drive(input);
      write(result);
    }
    process.exitCode = result.ok ? 0 : 1;
  })().catch(error => {
    process.stdout.write(JSON.stringify({ok:false,uncertain:false,...(process.argv[2]==='--listen'?{event:'fault'}:{}),
      error:/^[a-z_]+$/.test(error.message) ? error.message : 'browser_start_failed'}) + '\n');
    process.exitCode = 1;
  });
}
module.exports = {drive,pageFault,sourceRanges};
