// The Python host traces this process and every callback through the shared SDK.
import readline from 'node:readline';
import fs from 'node:fs';
import {createAgentSession, SessionManager, SettingsManager, ModelRuntime, DefaultResourceLoader} from '@earendil-works/pi-coding-agent';
const piVersion=JSON.parse(fs.readFileSync(new URL('../node_modules/@earendil-works/pi-coding-agent/package.json',import.meta.url),'utf8')).version;

const input = readline.createInterface({input: process.stdin});
const pending = new Map(); let sequence = 0, session, stopRequested=false;
function send(value) { process.stdout.write(JSON.stringify(value) + '\n'); }
function call(method, body) {
  const id = String(++sequence);
  return new Promise((resolve, reject) => { pending.set(id, {resolve, reject}); send({type:'call', id, method, body}); });
}
async function start(config) {
  let compacting=false;
  const modelRuntime = await ModelRuntime.create({modelsPath:null, authPath:config.agentDir+'/auth.json',
    modelsStorePath:config.agentDir+'/models-cache.json', allowModelNetwork:false, refreshOnCreate:false});
  // Native Pi compaction uses this same runtime, including after finish_task.
  // Capture the purpose synchronously, before the provider's lazy stream starts.
  for (const method of ['stream','streamSimple']) {
    const original=modelRuntime[method].bind(modelRuntime);
    modelRuntime[method]=(model,context,options)=>original(model,context,{...options,
      headers:{...options?.headers,'X-Drama-Pi-Purpose':compacting?'compaction':'decision'}});
  }
  modelRuntime.registerProvider('drama-gateway', {baseUrl:config.baseUrl, apiKey:config.token,
    api:'openai-completions', models:[{id:config.model, name:config.model, reasoning:false, input:['text','image'],
      contextWindow:config.contextWindow || 131072, maxTokens:config.maxTokens,
      cost:{input:0,output:0,cacheRead:0,cacheWrite:0}, compat:{supportsDeveloperRole:false, supportsStore:false}}]});
  const settingsManager = SettingsManager.inMemory({retry:{enabled:false,provider:{maxRetries:0}},
    compaction:{enabled:true,reserveTokens:16384,keepRecentTokens:12000}});
  const manager = config.sessionFile && fs.existsSync(config.sessionFile)
    ? SessionManager.open(config.sessionFile,config.sessionDir)
    : SessionManager.create(config.cwd,config.sessionDir);
  const loader = new DefaultResourceLoader({cwd:config.cwd,agentDir:config.agentDir,
    noExtensions:true,noSkills:true,noPromptTemplates:true,noThemes:true,
    // The host supplies the story role. Repository AGENTS/CLAUDE files guide contributors.
    noContextFiles:true, systemPrompt:config.system});
  await loader.reload();
  let completed=false;
  const customTools = config.tools.map(tool => ({name:tool.name,label:tool.name,description:tool.description,
    parameters:tool.parameters,executionMode:'sequential',
    async execute(toolCallId,args) {
      const result = await call('tool',{toolCallId,name:tool.name,args});
      if(tool.name==='finish_task' && !result.finish_rejected)completed=true;
      return {content:[{type:'text',text:JSON.stringify(result)}],details:result};
    }}));
  ({session} = await createAgentSession({cwd:config.cwd,agentDir:config.agentDir,modelRuntime,
    model:modelRuntime.getModel('drama-gateway',config.model),thinkingLevel:'off',settingsManager,
    sessionManager:manager,resourceLoader:loader,tools:config.tools.map(t=>t.name),customTools}));
  session.agent.finishTurn=()=>completed||stopRequested?{action:'end'}:undefined;
  await call('ready',{sessionFile:manager.getSessionFile(),sessionId:manager.getSessionId(),
    backend:'pi',version:piVersion,messages:session.messages.length});
  let publicBuffer='',publicMessageId='',lastPublicFlush=0;
  async function flushPublicText(){
    if(!publicBuffer)return;
    const text=publicBuffer;publicBuffer='';lastPublicFlush=Date.now();
    await call('event',{type:'assistant_text',message_id:publicMessageId,text});
  }
  session.subscribe(async event => {
    if(event.type==='compaction_start')compacting=true;
    if(event.type==='compaction_end')compacting=false;
    if(event.type==='message_update' && event.assistantMessageEvent?.type==='text_delta'){
      publicMessageId=String(event.message?.timestamp||publicMessageId);
      publicBuffer+=event.assistantMessageEvent.delta||'';
      if(publicBuffer.length>=256||Date.now()-lastPublicFlush>=200)await flushPublicText();
    } else if (event.type==='message_end') {
      await flushPublicText();
      const m=event.message;
      // Hidden reasoning is deliberately excluded from project session persistence.
      if (Array.isArray(m?.content)) m.content=m.content.filter(c=>c.type!=='thinking');
      await call('event',{type:'message_end',message:m});
    } else if (['compaction_start','compaction_end','agent_settled','tool_execution_start','tool_execution_end'].includes(event.type)) {
      await call('event',{type:event.type,toolName:event.toolName,success:event.success,error:event.errorMessage||event.error,
        reason:event.reason,aborted:event.aborted,willRetry:event.willRetry});
    }
  });
  await prompt(config.prompt);
}
async function prompt(text) {
  try {
    await session.prompt(text);
    await call('settled',{text:session.getLastAssistantText(),sessionFile:session.sessionManager?.getSessionFile?.()});
  } catch(error) { send({type:'fatal',error:String(error?.stack || error)}); }
}
input.on('line',line=>{
  let data;try{data=JSON.parse(line);}catch{send({type:'fatal',error:'Invalid host JSON'});return;}
  if(data.type==='reply') {const p=pending.get(data.id);if(p){pending.delete(data.id);data.error?p.reject(new Error(data.error)):p.resolve(data.result);}return;}
  if(data.type==='start') void start(data.config).catch(error=>send({type:'fatal',error:String(error?.stack||error)}));
  else if(data.type==='steer' && session) void session.steer(data.text);
  else if(data.type==='prompt' && session) void prompt(data.text);
  else if(data.type==='stop' && session) stopRequested=true;
  else if(data.type==='close') {session?.dispose();input.close();process.exit(0);}
});
