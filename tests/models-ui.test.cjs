/* Interaction tests against app.js with a minimal DOM/HTTP harness, not visual QA. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.join(__dirname,'../isc_agents/platform/static');
const html=fs.readFileSync(path.join(root,'index.html'),'utf8');
const source=fs.readFileSync(path.join(root,'app.js'),'utf8');

async function browser(live=false,fixture={}){
  const elements=new Map(),requests=[],profileData={profiles:[],selected_id:'',server_default:null};
  for(const match of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)){
    const tag=match[0],classes=new Set();
    elements.set(match[1],{id:match[1],value:tag.match(/\bvalue="([^"]*)"/)?.[1]||'',checked:/\schecked(?:\s|>)/.test(tag),hidden:/\shidden(?:\s|>)/.test(tag),disabled:false,dataset:{},events:{},textContent:'',innerHTML:'',
      classList:{toggle(name,force){if(force)classes.add(name);else classes.delete(name);}},
      addEventListener(name,callback){(this.events[name]??=[]).push(callback);},removeAttribute(){},setAttribute(){}});
  }
  elements.get('model-provider').value='local';elements.get('model-disable-thinking').checked=true;
  const envs=live?[{id:'sandbox',name:'Sandbox',client:'test',stage:'sandbox',backend:'isc',base_url:'https://test.api.identitynow.com'}]:[];
  const fetch=async(url,options)=>{
    const body=options.body?JSON.parse(options.body):null;requests.push({url,method:options.method,body});
    let value=[];
    if(url==='/api/me')value={csrf:'synthetic-csrf',user:{id:'admin',role:'admin'},model_configured:false};
    else if(url==='/api/environments')value=envs;
    else if(url==='/api/model-profiles'&&options.method==='GET')value=profileData;
    else if(url==='/api/model-profiles'&&options.method==='POST'){
      const {api_key,...publicFields}=body;value={id:'local-profile',...publicFields,revision:'model-revision',has_api_key:false};profileData.profiles=[value];
    }else if(url==='/api/model-selection'){profileData.selected_id=body.profile_id;value={selected_model:profileData.profiles[0]};}
    else if(url==='/api/environments/sandbox/plan')value={job_id:'local-plan-job',task:'plan'};
    else if(url==='/api/jobs'&&fixture.lookupCompleted)value=[{id:'local-plan-job',environment_id:'sandbox',state:'completed',result:{answer_id:'answer-1'}}];
    else if(url==='/api/environments/sandbox/answers/answer-1')value={kind:'agent_answer',status:'answered',answer:'<img src=x> Jane Doe: jane@example.com',model_calls:3,duration_seconds:0.1,evidence:[]};
    return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(value))};
  };
  const context=vm.createContext({document:{getElementById:id=>elements.get(id),querySelectorAll:()=>[],hidden:false},fetch,console,
    sessionStorage:{getItem:()=>'',setItem(){}},setTimeout:()=>1,clearTimeout(){},setInterval:()=>1,Blob,URL});
  vm.runInContext(source,context);await new Promise(setImmediate);
  async function event(id,name){for(const callback of elements.get(id).events[name]||[])await callback({preventDefault(){},target:elements.get(id)});await new Promise(setImmediate);}
  return {elements,requests,context,event};
}

test('a keyless local model is saved, selected, and used by the next planning request',async()=>{
  const b=await browser(true);
  b.elements.get('model-name').value='Local Qwen';b.elements.get('model-id').value='qwen';
  await b.event('model-form','submit');
  const save=b.requests.find(r=>r.url==='/api/model-profiles'&&r.method==='POST');
  assert.equal(save.body.provider,'local');assert.equal(save.body.api_key,'');assert.equal(save.body.base_url,'http://127.0.0.1:8080/v1');
  assert.equal(b.elements.get('planning-model-select').value,'local-profile');
  assert.match(b.elements.get('metric-model').textContent,/Local.*qwen/);
  b.elements.get('prompt').value='Read my tenant configuration';
  await b.event('plan-form','submit');
  const plan=b.requests.find(r=>r.url==='/api/environments/sandbox/plan');
  assert.equal(plan.body.model_profile_id,'local-profile');assert.equal(plan.body.external,false);
  assert.equal(b.elements.get('model-api-key').value,'');
});

test('provider changes clear typed keys and toggle local endpoint controls',async()=>{
  const b=await browser();
  b.elements.get('model-provider').value='openai';await b.event('model-provider','change');
  assert.equal(b.elements.get('local-model-fields').hidden,true);
  b.elements.get('model-api-key').value='synthetic-cloud-key';b.elements.get('model-provider').value='local';
  await b.event('model-provider','change');
  assert.equal(b.elements.get('model-api-key').value,'');assert.equal(b.elements.get('local-model-fields').hidden,false);
});

test('a live request without a model guides the user to model setup without queuing a failure',async()=>{
  const b=await browser(true);b.elements.get('prompt').value='Inspect the source';
  await b.event('plan-form','submit');
  assert.equal(vm.runInContext('state.tab',b.context),'models');
  assert.equal(b.requests.some(r=>r.url==='/api/environments/sandbox/plan'),false);
});


test('an identity example still requires an LLM to choose its tools',async()=>{
  const b=await browser(true);b.elements.get('prompt').value='Retrieve information about Jane Doe';
  await b.event('plan-form','submit');
  assert.equal(b.requests.some(r=>r.url==='/api/environments/sandbox/plan'),false);
  assert.equal(vm.runInContext('state.tab',b.context),'models');
});

test('examples only populate editable prompt text without selecting a business route',async()=>{
  const b=await browser(true);b.elements.get('task-example').value='identity';
  await b.event('task-example','change');
  assert.match(b.elements.get('prompt').value,/Jane Doe/);
  assert.equal(b.elements.has('request-task'),false);
  assert.equal(b.elements.has('identity-query'),false);
});

test('a completed agent response renders escaped text without approval controls',async()=>{
  const b=await browser(true,{lookupCompleted:true});
  await b.event('model-form','submit');
  b.elements.get('prompt').value='Retrieve information about Jane Doe';
  await b.event('plan-form','submit');
  const result=b.elements.get('answer-detail').innerHTML;
  assert.match(result,/jane@example.com/);assert.match(result,/3 model calls/);
  assert.match(result,/&lt;img src=x&gt;/);assert.doesNotMatch(result,/<img/);
  assert.doesNotMatch(result,/Approve exact plan/);
});
