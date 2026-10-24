'use strict';
const $ = id => document.getElementById(id);
const state = {csrf: '', me: null, envs: [], specs: [], eid: '', editing: '', run: null, selectedRun: '', jobs: [], tab: 'workspace', pendingPlan: '', selectedAnswer:'', audit: [], modelData: {profiles:[],selected_id:'',server_default:null}, editingModel:'', testingModels:new Set()};
const sample = {source_name:'Example HR',connector:'delimited-file',authoritative:true,account_id:'employeeId',correlation_attribute:'employeeId',attributes:['employeeId','firstName','lastName','email','department','status'],mappings:[{source:'employeeId',identity:'employeeNumber'},{source:'email',identity:'email'},{source:'department',identity:'department'},{source:'status',identity:'employmentStatus'}]};
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pretty = value => JSON.stringify(value, null, 2);
const stamp = at => new Date(at * 1000).toLocaleString();
const badge = value => `<span class="badge ${esc(value)}">${esc(String(value).replaceAll('_',' '))}</span>`;
const env = () => state.envs.find(e => e.id === state.eid);
let toastTimer;
function toast(message, error=false) { $('toast').textContent=message; $('toast').classList.toggle('error',error); $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,7000); }
async function api(path, method='GET', body) {
  const response=await fetch('/api'+path,{method,credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':state.csrf},...(body!==undefined?{body:JSON.stringify(body)}:{})});
  const result=await response.json().catch(()=>({error:'Server returned an unexpected response'}));
  if(!response.ok){if(response.status===401){state.me=null;$('app').hidden=true;$('login-screen').hidden=false;}throw new Error(result.error || 'Request failed');}return result;
}
function environmentStatus(message,error=false){const status=$('environment-status');status.textContent=message;status.hidden=!message;status.classList.toggle('error',error);}
function on(id,event,handler){$(id).addEventListener(event,async e=>{if(event==='submit')e.preventDefault();try{await handler(e);}catch(error){if(id==='environment-form')environmentStatus(error.message,true);if(id==='model-form')modelStatus(error.message,true);toast(error.message,true);}});}
function requireEnv(){if(!env())throw new Error('Select an environment first');return env();}
function download(name,content,type='application/json'){const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([content],{type}));a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);}
function table(headers,rows){return rows.length?`<div class="table-scroll"><table><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(c=>`<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`:'<p class="empty">No activity yet.</p>';}
function switchTab(tab){state.tab=tab;document.querySelectorAll('.tab').forEach(e=>e.hidden=e.id!=='tab-'+tab);document.querySelectorAll('.nav').forEach(e=>e.classList.toggle('active',e.dataset.tab===tab));$('page-name').textContent={workspace:'Workspace',models:'Models & appearance',environments:'Environments',knowledge:'Knowledge & research',tools:'Tools & specialists',audit:'Audit trail'}[tab];refresh().catch(e=>toast(e.message,true));}
document.querySelectorAll('.nav').forEach(button=>button.addEventListener('click',()=>switchTab(button.dataset.tab)));

on('login-form','submit',async()=>{try{const login=await api('/login','POST',{username:$('username').value,password:$('password').value});state.csrf=login.csrf;$('password').value='';$('login-error').textContent='';await boot();}catch(e){$('login-error').textContent=e.message;}});
on('logout','click',async()=>{await api('/logout','POST',{});state.me=null;state.csrf='';state.selectedRun='';state.run=null;state.pendingPlan='';state.selectedAnswer='';$('answer-detail').innerHTML='';$('app').hidden=true;$('login-screen').hidden=false;});

async function boot(){state.me=await api('/me');state.csrf=state.me.csrf;$('user-badge').textContent=state.me.user.id.slice(0,2).toUpperCase();$('user-badge').title=state.me.user.id+' · '+state.me.user.role;$('app').hidden=false;$('login-screen').hidden=true;$('metric-model').textContent=state.me.model_configured?state.me.model:'Manual / offline mode';state.eid=sessionStorage.getItem('isc-environment')||'';await refresh();}
function renderEnvironments(){
  if(!state.envs.some(e=>e.id===state.eid))state.eid=state.envs[0]?.id||'';
  $('environment-select').innerHTML='<option value="">Select an environment</option>'+state.envs.map(e=>`<option value="${esc(e.id)}">${esc(e.name)}</option>`).join('');$('environment-select').value=state.eid;
  const current=env();$('environment-badge').className='badge '+(current?.stage||'');$('environment-badge').textContent=current?`${current.stage} · ${current.backend==='isc'?'live':'simulation'}`:'No environment';
  $('metric-env').textContent=current?.name||'Not selected';$('metric-tenant').textContent=current?.base_url||'Local simulated tenant';$('metric-policy').textContent=current?.backend==='isc'?(current.write_enabled?'Approved allowlisted writes':'Read-only tenant'):'Simulated execution';
  $('plan-hint').textContent=!current?'Choose an environment to begin.':current.backend==='simulator'?'Offline simulation · explicit source requirements':state.me.model_configured?'Agent planning · no writes before approval':'Choose a local or cloud model in Models & appearance';
  $('plan-button').disabled=!current;$('discover').disabled=current?.backend!=='isc';
  $('environment-cards').innerHTML=state.envs.length?state.envs.map(e=>`<article class="environment-card ${e.id===state.eid?'active':''}"><strong>${esc(e.name)}</strong> ${badge(e.stage)}<p>${esc(e.client)} · ${esc(e.backend)}<br>${esc(e.base_url||'Local simulator')}</p><p>${e.has_credentials?'Encrypted credentials configured':'No live credentials'} · ${e.write_enabled?'Approved writes enabled':'Read-only policy'}</p><button class="text-button" data-env-edit="${esc(e.id)}">Edit environment →</button></article>`).join(''):'<p class="empty">Create your first environment.</p>';
  document.querySelectorAll('[data-env-edit]').forEach(b=>b.addEventListener('click',()=>editEnvironment(b.dataset.envEdit)));
  requestHelp();
  const currentSpec=$('env-spec').value;
  $('env-spec').innerHTML='<option value="">Select imported specification</option>'+state.specs.map(s=>`<option value="${esc(s.id)}">${esc(s.version)} · ${esc(s.hash.slice(0,10))} · ${esc(stamp(s.fetched))}</option>`).join('');
  if(state.specs.some(s=>s.id===currentSpec))$('env-spec').value=currentSpec;
}
on('environment-select','change',async()=>{state.eid=$('environment-select').value;sessionStorage.setItem('isc-environment',state.eid);state.selectedRun='';state.run=null;state.pendingPlan='';state.selectedAnswer='';$('answer-detail').innerHTML='';renderEmptyRun();setStructuredDefault();await refresh();});
function setStructuredDefault(){if(env()?.backend==='simulator'){$('structured').value=pretty(sample);$('requirements-details').open=true;$('prompt').value='Simulate an authoritative delimited-file HR source configuration.';}else{$('structured').value='';$('requirements-details').open=false;$('prompt').value='';}}
function editEnvironment(eid=''){state.editing=eid;const e=state.envs.find(x=>x.id===eid);$('env-form-title').textContent=e?'Edit environment':'Create environment';$('env-name').value=e?.name||'';$('env-client').value=e?.client||'';$('env-stage').value=e?.stage||'sandbox';$('env-backend').value=e?.backend||'simulator';$('env-base-url').value=e?.base_url||'';$('env-spec').value=e?.spec_id||state.specs[0]?.id||'';$('env-writes').checked=e?.write_enabled||false;$('env-operations').value=(e?.allowed_write_operations||[]).join('\n');for(const id of ['env-client-id','env-client-secret','env-connector-secrets'])$(id).value='';$('credential-hint').textContent=e?.has_credentials?'Credentials already configured. Leave credential fields empty to retain them.':'Secrets are encrypted and never returned to the browser or model.';}
on('new-environment','click',()=>editEnvironment());
$('environment-form').addEventListener('invalid',e=>{const message=e.target.id==='env-client'&&e.target.validity.patternMismatch?'Client identifier cannot contain spaces. Use an internal identifier such as magellan_lab; this is separate from your OAuth Client ID.':e.target.validationMessage;e.target.setAttribute('aria-invalid','true');environmentStatus(message,true);},true);
$('environment-form').addEventListener('input',e=>{e.target.removeAttribute('aria-invalid');environmentStatus('');});
on('environment-form','submit',async()=>{
  environmentStatus('');
  const payload={name:$('env-name').value,client:$('env-client').value,stage:$('env-stage').value,backend:$('env-backend').value,base_url:$('env-base-url').value,client_id:$('env-client-id').value,client_secret:$('env-client-secret').value,spec_id:$('env-spec').value,write_enabled:$('env-writes').checked,allowed_write_operations:$('env-operations').value.split(/[\n,]/).map(s=>s.trim()).filter(Boolean)};
  if($('env-connector-secrets').value.trim())payload.connector_secrets=JSON.parse($('env-connector-secrets').value);
  const saved=await api('/environments'+(state.editing?'/'+state.editing:''),state.editing?'PUT':'POST',payload);state.eid=saved.id;sessionStorage.setItem('isc-environment',state.eid);await refresh();editEnvironment(saved.id);setStructuredDefault();environmentStatus('Environment saved successfully.');toast('Environment saved. Earlier plans retain their old revision.');
});
on('user-form','submit',async()=>{const e=requireEnv();await api('/users','POST',{username:$('new-user-name').value,password:$('new-user-password').value,role:$('new-user-role').value,environments:[e.id]});$('new-user-password').value='';$('user-form').reset();toast('User created for '+e.name);});
function requestMode(){return $('request-task').value||'auto';}
function likelyLookup(prompt){return /^(?:(?:retrieve|get|show|find|look up|lookup)\s+(?:information|info|details|a summary)\s+(?:about|for|on)\s+|(?:find|show|lookup|look up|get)\s+(?:the\s+)?(?:identity|person|user)\s+|who is\s+)/i.test(prompt.trim().replace(/^please\s+/i,''));}
function requestHelp(){
  const task=requestMode();$('identity-query-field').hidden=task!=='identity_lookup';
  $('plan-hint').textContent=!env()?'Choose an environment to begin.':env().backend==='simulator'?'Offline simulation · explicit source requirements':state.me?.model_configured?'Agent planning · no writes before approval':'Choose a local or cloud model in Models & appearance';
  if(env()?.backend==='isc'&&(task==='identity_lookup'||(task==='auto'&&likelyLookup($('prompt').value))))$('plan-hint').textContent='Read-only identity profile · no model needed';
  else if(task==='explanation')$('plan-hint').textContent='Indexed documentation · one model call';
  $('request-help').textContent=task==='identity_lookup'?'Search exact names, aliases, emails, or identity IDs. Multiple matches require your selection. No LLM, account, entitlement, or role-mining requests.':task==='explanation'?'Answer from indexed documentation in one model call. Ingest relevant pages under Knowledge & research first; this mode does not browse the web.':task==='integration'?'Describe the connector, ownership, business rules, and expected outcome. Agents research and propose a plan for review.':'Automatic routing recognizes simple identity lookups and documentation questions. Choose a task explicitly for ambiguous requests.';
}
on('request-task','change',requestHelp);
on('prompt','input',requestHelp);
on('task-example','change',()=>{
  const examples={identity:{task:'identity_lookup',prompt:'Retrieve information about Jane Doe',query:'Jane Doe'},documentation:{task:'explanation',prompt:'Explain how account correlation works in SailPoint ISC.'},connector:{task:'integration',prompt:'Plan a delimited-file HR source integration. Employee ID is the account identifier and correlation key. Include schema, ownership, prerequisites, and verification; ask for missing business decisions.'}};
  const example=examples[$('task-example').value];if(!example)return;
  $('request-task').value=example.task;$('prompt').value=example.prompt;$('identity-query').value=example.query||'';requestHelp();
});
on('plan-form','submit',async()=>{
  const e=requireEnv(),structured=$('structured').value.trim()?JSON.parse($('structured').value):null;
  const task=requestMode(),lookup=task==='identity_lookup'||(task==='auto'&&likelyLookup($('prompt').value));
  if(e.backend==='isc'&&!structured&&!lookup&&!state.me.model_configured){switchTab('models');toast('Add a local llama.cpp profile or choose a cloud model to enable agent planning.');return;}
  const payload={prompt:$('prompt').value,external:$('external').checked,model_profile_id:state.modelData.selected_id||'',task,identity_query:task==='identity_lookup'?$('identity-query').value:''};
  if(e.backend==='simulator')payload.requirements=structured||sample;else if(structured)payload.proposal=structured;
  const result=await api(`/environments/${e.id}/plan`,'POST',payload);state.pendingPlan=result.job_id;
  toast(result.task==='identity_lookup'?'Identity lookup queued · no model calls.':result.task==='explanation'?'Documentation answer queued.':'Planning job queued. Follow its progress in Background activity.');await refresh();
});
on('discover','click',async()=>{const e=requireEnv();await api(`/environments/${e.id}/discover`,'POST',{});toast('Read-only discovery queued.');await refresh();});
on('csv-upload','change',async()=>{const e=requireEnv(),file=$('csv-upload').files[0];if(!file)return;if(file.size>5000000)throw new Error('CSV exceeds the 5 MB limit');const bytes=new Uint8Array(await file.arrayBuffer());let text='';for(let i=0;i<bytes.length;i+=16384)text+=String.fromCharCode(...bytes.subarray(i,i+16384));await api(`/environments/${e.id}/attachments`,'POST',{name:file.name,base64:btoa(text)});$('csv-upload').value='';toast('CSV encrypted and attached to '+e.name);await refresh();});
on('refresh','click',refresh);
function renderEmptyRun(){$('run-detail').innerHTML='<div class="empty large">◈<h3>A clear record of every change</h3><p>Select a run to inspect its plan, evidence, and execution results.</p></div>';}
async function loadRun(rid,eid=state.eid){const run=await api(`/environments/${eid}/runs/${rid}`);if(eid!==state.eid||rid!==state.selectedRun)return;state.run=run;renderRun(run,eid);}
function renderRun(run,eid){
  const approved=run.status==='planned',executable=['approved','running'].includes(run.status),canReview=['admin','reviewer'].includes(state.me.user.role),canExecute=state.me.user.role!=='reviewer';
  $('run-detail').innerHTML=`<div class="card-heading"><h3>${esc(run.plan.title||'Clarification needed')}</h3>${badge(run.status)}</div><p class="digest">Plan SHA-256 · ${esc(run.hash)}</p>${run.error?`<p class="notice error">${esc(run.error)}</p>`:''}${(run.plan.questions||[]).length?`<div class="notice"><strong>Decisions needed</strong><ul>${run.plan.questions.map(q=>`<li>${esc(q)}</li>`).join('')}</ul><p>Update your request with these answers and create a new plan.</p></div>`:''}<div class="run-actions"><button class="primary" id="approve-run" ${approved&&canReview?'':'disabled'}>Approve exact plan</button><button class="secondary" id="execute-run" ${executable&&canExecute?'':'disabled'}>Execute / resume</button><button class="text-button" id="revoke-run" ${executable&&canReview?'':'disabled'}>Revoke / renew</button><button class="text-button" id="export-run">Export JSON ↓</button></div><p class="fine">Review the complete plan below before approving. Approval does not start execution.</p>${(run.plan.steps||[]).map((s,i)=>`<div class="step-row"><div><strong>${String(i+1).padStart(2,'0')} · ${esc(s.id)}</strong><br><span class="fine">${esc(s.operation_id)}</span></div>${badge(run.steps[i]?.state||'pending')}</div>`).join('')}<details open><summary>Review findings</summary><pre>${esc(pretty(run.plan.review||{}))}</pre></details><details><summary>Complete plan & evidence</summary><pre>${esc(pretty(run.plan))}</pre></details><details><summary>Execution results</summary><pre>${esc(pretty(run.steps))}</pre></details>${['failed','needs_reconciliation'].includes(run.status)?'<details><summary>Read-only reconciliation (administrator)</summary><p class="fine">Provide operation_id, arguments, the complete expected read result, and decision: confirm_applied or abandon. This records observed state; it never replays an uncertain write.</p><textarea id="reconcile-json" class="code-input" rows="7" aria-label="Reconciliation JSON"></textarea><button class="secondary" id="reconcile-run">Check evidence & reconcile</button></details>':''}`;
  on('approve-run','click',async()=>{await api(`/environments/${eid}/runs/${run.id}/approve`,'POST',{hash:run.hash});toast('Exact plan approved. Execution remains a separate action.');await refresh();});
  on('execute-run','click',async()=>{await api(`/environments/${eid}/runs/${run.id}/execute`,'POST',{});toast('Execution queued. Inspect persisted step results.');await refresh();});
  on('revoke-run','click',async()=>{await api(`/environments/${eid}/runs/${run.id}/revoke`,'POST',{});toast('Approval revoked. Review and approve again to renew.');await refresh();});
  on('export-run','click',()=>download('integration-run-'+run.id+'.json',pretty(run)));
  if($('reconcile-run'))on('reconcile-run','click',async()=>{await api(`/environments/${eid}/runs/${run.id}/reconcile`,'POST',JSON.parse($('reconcile-json').value));toast('Reconciliation evidence recorded.');await refresh();});
}
on('sync-specs','click',async()=>{await api('/specs/sync','POST',{});toast('Official API release discovery and ingestion queued.');await refresh();});
on('crawl-form','submit',async()=>{await api('/ingestion','POST',{seeds:$('crawl-seeds').value.split('\n').map(x=>x.trim()).filter(Boolean),max_pages:Number($('crawl-pages').value),daily:$('crawl-daily').checked});toast('Documentation crawl queued. Check Background activity.');switchTab('workspace');});
on('search-form','submit',async()=>{const e=requireEnv(),results=await api(`/environments/${e.id}/knowledge/search`,'POST',{query:$('search-query').value});$('search-results').innerHTML=results.length?results.map(r=>`<article class="evidence">${badge(r.trust)} <a href="${esc(r.url.startsWith('https://')?r.url:'#')}" target="_blank" rel="noopener noreferrer">${esc(r.title)} ↗</a><p>${esc(r.content)}</p><span class="fine">Fetched ${esc(stamp(r.fetched))} · ${esc(r.hash.slice(0,12))}</span></article>`).join(''):'<p class="empty">No matching evidence. Import documentation or client guidance.</p>';});
on('research-form','submit',async()=>{const e=requireEnv(),value=$('research-query').value;const result=await api(`/environments/${e.id}/research`,'POST',{external:$('research-consent').checked,...(value.startsWith('https://')?{url:value}:{query:value})});$('research-results').textContent=pretty(result);});
on('client-doc-form','submit',async()=>{const e=requireEnv();await api(`/environments/${e.id}/knowledge/import`,'POST',{title:$('client-doc-title').value,content:$('client-doc-content').value});toast('Client guidance indexed for '+e.name);$('client-doc-form').reset();});
on('catalog-form','submit',async()=>{const e=requireEnv();$('catalog-results').textContent=pretty(await api(`/environments/${e.id}/operations?query=`+encodeURIComponent($('catalog-query').value)));});
on('export-audit','click',()=>download('audit-'+requireEnv().id+'.json',pretty(state.audit)));

let refreshing=false;
async function refresh(){
  if(!state.me||refreshing)return;
  refreshing=true;
  try {
  const [envs,specs,jobs,models]=await Promise.all([api('/environments'),api('/specs'),api('/jobs'),api('/model-profiles')]);state.envs=envs;state.specs=specs;state.jobs=jobs;state.modelData=models;renderModels();const wasEmpty=!state.eid;renderEnvironments();if(wasEmpty&&state.eid)setStructuredDefault();
  $('jobs').innerHTML=table(['JOB','STATE','UPDATED','RESULT / PROGRESS'],jobs.slice(0,12).map(j=>[esc(j.kind),badge(j.state),esc(stamp(j.updated)),esc(j.error||pretty(j.result?.progress||j.result||{}))+(j.result?.activity?.length?`<details><summary>Agent activity (${j.result.activity.length})</summary><pre>${esc(pretty(j.result.activity))}</pre></details>`:'')]));
  $('specs').innerHTML=table(['VERSION','CONTENT HASH','IMPORTED','ORIGIN'],specs.map(s=>[esc(s.version),esc(s.hash.slice(0,16)),esc(stamp(s.fetched)),`<a href="${esc(s.origin)}" target="_blank" rel="noopener noreferrer">Official specification ↗</a>`]));
  if(!state.eid){$('answers').innerHTML='';$('answer-detail').innerHTML='';$('runs').innerHTML='<p class="empty">Create an environment to begin.</p>';return;}
  const eid=state.eid;
  const [runs,attachments,answers]=await Promise.all([api(`/environments/${eid}/runs`),api(`/environments/${eid}/attachments`),api(`/environments/${eid}/answers`)]);if(state.eid!==eid)return;
  const pending=jobs.find(j=>j.id===state.pendingPlan&&j.environment_id===eid);if(pending?.state==='completed'&&pending.result?.run_id){state.selectedRun=pending.result.run_id;state.pendingPlan='';}else if(pending?.state==='completed'&&pending.result?.answer_id){state.selectedAnswer=pending.result.answer_id;state.pendingPlan='';}else if(pending?.state==='failed'){toast(pending.error||'Planning failed',true);state.pendingPlan='';}
  renderAnswers(answers,eid);
  if(state.selectedAnswer)await loadAnswer(state.selectedAnswer,eid);
  $('runs').innerHTML=runs.length?runs.map(r=>`<button class="run-item ${r.id===state.selectedRun?'active':''}" data-run="${esc(r.id)}">${badge(r.status)}<strong>${esc(r.id.slice(0,16))}</strong><p>${esc(r.creator)} · ${esc(stamp(r.created))}</p></button>`).join(''):'<p class="empty">Your integration plans will appear here.</p>';
  document.querySelectorAll('[data-run]').forEach(b=>b.addEventListener('click',()=>{state.selectedRun=b.dataset.run;loadRun(state.selectedRun).catch(e=>toast(e.message,true));}));
  $('attachments').textContent=attachments.map(a=>`${a.name} · ${a.size.toLocaleString()} bytes · ${a.id}`).join(' | ');
  if(state.selectedRun)await loadRun(state.selectedRun,eid);
  if(state.tab==='tools'){
    const [tools,drafts]=await Promise.all([api(`/environments/${eid}/tools`),api(`/environments/${eid}/drafts`)]);if(eid!==state.eid)return;
    $('tools').innerHTML=tools.length?tools.map(t=>`<article class="artifact"><h3>${esc(t.manifest.name)} ${badge(t.status)}</h3><p class="fine">${esc(t.manifest.description)}</p><pre>${esc(pretty(t.manifest))}</pre><p class="digest">${esc(t.hash)}</p>${t.status==='proposed'?`<button class="secondary" data-activate="${esc(t.id)}">Activate reviewed tool</button>`:''}</article>`).join(''):'<p class="empty">Agent-proposed API wrappers will appear here for review.</p>';
    document.querySelectorAll('[data-activate]').forEach(b=>b.addEventListener('click',async()=>{try{const t=tools.find(x=>x.id===b.dataset.activate);await api(`/environments/${eid}/tools/${t.id}/activate`,'POST',{hash:t.hash});toast('Reviewed tool activated within environment permissions.');await refresh();}catch(e){toast(e.message,true);}}));
    $('drafts').innerHTML=drafts.length?drafts.map(d=>`<article class="artifact"><h3>${esc(d.name)}</h3><p class="fine">Human review required · not executed</p><details><summary>Inspect code and static checks</summary><pre>${esc(d.content)}</pre><pre>${esc(pretty(d.review))}</pre></details><button class="secondary" data-download-draft="${esc(d.id)}">Download draft ↓</button></article>`).join(''):'<p class="empty">No generated code drafts.</p>';
    document.querySelectorAll('[data-download-draft]').forEach(b=>b.addEventListener('click',()=>{const d=drafts.find(x=>x.id===b.dataset.downloadDraft);download(d.name,d.content,'text/plain');}));
  }
  if(state.tab==='audit'){state.audit=await api(`/environments/${eid}/audit`);$('audit').innerHTML=table(['TIME','ACTOR','EVENT','DETAILS'],state.audit.map(e=>[esc(stamp(e.at)),esc(e.actor),esc(e.kind),esc(pretty(e.details))]));}
  } finally {refreshing=false;}
}
function renderAnswers(answers,eid){
  $('answers').innerHTML=answers.map(a=>`<button class="secondary" data-answer="${esc(a.id)}">${esc(a.kind==='identity_lookup'?'Identity lookup':'Documentation')} · ${esc(stamp(a.created))}</button>`).join('');
  document.querySelectorAll('[data-answer]').forEach(b=>b.addEventListener('click',()=>{state.selectedAnswer=b.dataset.answer;loadAnswer(state.selectedAnswer,eid).catch(e=>toast(e.message,true));}));
}
async function loadAnswer(aid,eid){
  const answer=await api(`/environments/${eid}/answers/${aid}`);if(eid!==state.eid||aid!==state.selectedAnswer)return;
  const fields=answer.identity||{};
  $('answer-detail').innerHTML=`<div class="card-heading"><h3>${esc(answer.kind==='identity_lookup'?'Identity profile':'Documentation answer')}</h3>${badge(answer.status)}</div><p>${esc(answer.message||'')}</p>${answer.answer?`<p class="answer-text">${esc(answer.answer)}</p>`:''}${Object.keys(fields).length?table(['FIELD','VALUE'],Object.entries(fields).map(([k,v])=>[esc(k),esc(typeof v==='object'?pretty(v):v)])):''}${answer.candidates?.length?table(['NAME','EMAIL','IDENTITY ID','SELECT'],answer.candidates.map(c=>[esc([c.firstname,c.lastname].filter(Boolean).join(' ')||c.name),esc(c.email),esc(c.id),`<button class="secondary" data-identity-choice="${esc(c.id)}">Use this identity</button>`])):''}${answer.truncated?'<p class="notice">Only the first 20 candidates are shown. Refine with an email or identity ID.</p>':''}<p class="fine">${esc(answer.scope||answer.note||'')} · ${esc(answer.model_calls)} model calls · ${esc(answer.duration_seconds)} seconds</p><details><summary>API / documentation evidence</summary><pre>${esc(pretty(answer.evidence||[]))}</pre></details>`;
  document.querySelectorAll('[data-identity-choice]').forEach(b=>b.addEventListener('click',async()=>{try{const result=await api(`/environments/${eid}/answers/${aid}/select`,'POST',{identity_id:b.dataset.identityChoice});state.pendingPlan=result.job_id;toast('Reading the selected identity.');await refresh();}catch(e){toast(e.message,true);}}));
}
const providerNames={local:'Local · llama.cpp',openai:'OpenAI',anthropic:'Claude',gemini:'Gemini'};
function modelStatus(message,error=false){$('model-status').textContent=message;$('model-status').hidden=!message;$('model-status').classList.toggle('error',error);}
function selectedModel(){return state.modelData.profiles.find(p=>p.id===state.modelData.selected_id)||state.modelData.server_default;}
function renderModels(){
  const data=state.modelData,current=selectedModel();state.me.model_configured=!!current;state.me.model=current?.model||null;
  $('metric-model').textContent=current?`${providerNames[current.provider]} · ${current.model}`:'Choose a local or cloud model';
  const options=`<option value="">${esc(data.server_default?'Server default · '+data.server_default.model:'No model selected')}</option>`+data.profiles.map(p=>`<option value="${esc(p.id)}">${esc(p.name+' · '+p.model)}</option>`).join('');
  if($('planning-model-select').dataset.signature!==options){$('planning-model-select').innerHTML=options;$('planning-model-select').dataset.signature=options;}
  $('planning-model-select').value=data.selected_id||'';
  $('model-data-note').textContent=current?.provider==='local'?'Local inference · no OpenAI account needed. Studio connects to your saved llama.cpp endpoint; external research remains separate.':current?`${providerNames[current.provider]} receives selected context for reasoning. API credentials remain encrypted on the Studio backend.`:'Choose a saved model or configure local Qwen / Llama. No cloud subscription is required for local inference.';
  $('model-profiles').innerHTML=data.profiles.length?data.profiles.map(p=>`<article class="model-profile ${p.id===data.selected_id?'active':''}"><div class="card-heading"><h3>${esc(p.name)}</h3>${badge(p.id===data.selected_id?'selected':p.provider==='local'?'local':'cloud')}</div><p>${esc(providerNames[p.provider])} · <strong>${esc(p.model)}</strong></p><p>${esc(p.provider==='local'?p.base_url:(p.has_api_key?'Encrypted API key configured':'No API key'))}</p><div class="model-profile-actions"><button class="secondary" data-use-model="${esc(p.id)}">Use this model</button><button class="text-button" data-edit-model="${esc(p.id)}">Edit</button><button class="text-button" data-test-model="${esc(p.id)}" ${state.testingModels.has(p.id)?'disabled':''}>${state.testingModels.has(p.id)?'Testing…':'Test JSON connection'}</button><button class="text-button" data-delete-model="${esc(p.id)}">Delete</button></div></article>`).join(''):'<p class="empty">No model profiles yet. Start with local llama.cpp—no cloud API key needed.</p>';
  document.querySelectorAll('[data-edit-model]').forEach(b=>b.addEventListener('click',()=>editModel(b.dataset.editModel)));
  document.querySelectorAll('[data-use-model]').forEach(b=>b.addEventListener('click',()=>useModel(b.dataset.useModel).catch(e=>toast(e.message,true))));
  document.querySelectorAll('[data-test-model]').forEach(b=>b.addEventListener('click',()=>testModel(b.dataset.testModel).catch(e=>toast(e.message,true))));
  document.querySelectorAll('[data-delete-model]').forEach(b=>b.addEventListener('click',async()=>{try{await api('/model-profiles/'+b.dataset.deleteModel,'DELETE');if(state.editingModel===b.dataset.deleteModel)editModel();await refresh();toast('Model profile deleted.');}catch(e){toast(e.message,true);}}));
}
function modelProviderFields(clear=false){
  const provider=$('model-provider').value,isLocal=provider==='local';$('local-model-fields').hidden=!isLocal;$('model-base-url').disabled=!isLocal;
  $('model-key-label').textContent=isLocal?'Server API key (optional)':providerNames[provider]+' API key';$('model-api-key').placeholder=isLocal?'Leave empty for keyless local llama.cpp':'Enter API key; leave blank to retain a saved key';
  $('model-key-help').textContent=isLocal?'No cloud API key is needed. Only add a key if you enabled llama.cpp authentication.':'Keys are encrypted and write-only. Saving another provider never reuses the previous provider’s key.';
  $('model-id').placeholder=isLocal?'qwen (the alias passed to llama-server)':'Exact model ID supported by your provider';
  if(clear){$('model-api-key').value='';$('model-id').value=isLocal?'qwen':'';$('model-max-output').value=isLocal?'4096':'6000';$('model-max-input').value=isLocal?'48000':'100000';modelStatus('');}
}
function editModel(id=''){
  const p=state.modelData.profiles.find(x=>x.id===id);state.editingModel=p?.id||'';$('model-form-title').textContent=p?'Edit model profile':'Connect a model';$('model-name').value=p?.name||'Local Qwen';$('model-provider').value=p?.provider||'local';$('model-id').value=p?.model||'qwen';$('model-base-url').value=p?.base_url||'http://127.0.0.1:8080/v1';$('model-api-key').value='';$('model-clear-key').checked=false;$('model-max-output').value=p?.max_output_tokens||(p?.provider&&p.provider!=='local'?6000:4096);$('model-max-input').value=p?.max_input_chars||48000;$('model-max-turns').value=p?.max_planning_turns||16;$('model-disable-thinking').checked=p?.disable_thinking??true;$('test-model-button').disabled=!p;modelProviderFields();modelStatus('');
}
async function useModel(id){await api('/model-selection','POST',{profile_id:id});await refresh();toast('Planning model selected. Existing queued jobs keep their original model.');}
async function testModel(id){
  if(!id)throw new Error('Save the model profile first.');state.testingModels.add(id);$('test-model-button').disabled=true;modelStatus('Testing structured JSON output. Local inference may take a little time…');renderModels();
  try{const result=await api(`/model-profiles/${id}/test`,'POST',{});modelStatus(result.message+' · '+result.model);toast('Model connection and JSON schema test passed.');}
  catch(e){modelStatus(e.message,true);throw e;}
  finally{state.testingModels.delete(id);$('test-model-button').disabled=!state.editingModel;renderModels();}
}
on('configure-model','click',()=>switchTab('models'));
on('new-model-profile','click',()=>editModel());
on('model-provider','change',()=>modelProviderFields(true));
on('planning-model-select','change',()=>useModel($('planning-model-select').value));
on('test-model-button','click',()=>testModel(state.editingModel));
$('model-form').addEventListener('invalid',e=>modelStatus(e.target.validationMessage,true),true);
on('model-form','submit',async()=>{
  $('save-model-button').disabled=true;modelStatus('');
  try{const payload={name:$('model-name').value,provider:$('model-provider').value,model:$('model-id').value,api_key:$('model-api-key').value,clear_api_key:$('model-clear-key').checked,max_output_tokens:Number($('model-max-output').value),max_input_chars:Number($('model-max-input').value),max_planning_turns:Number($('model-max-turns').value),disable_thinking:$('model-disable-thinking').checked};if(payload.provider==='local')payload.base_url=$('model-base-url').value;
    const profile=await api('/model-profiles'+(state.editingModel?'/'+state.editingModel:''),state.editingModel?'PUT':'POST',payload);$('model-api-key').value='';await api('/model-selection','POST',{profile_id:profile.id});await refresh();editModel(profile.id);modelStatus('Profile saved and selected. Test the connection, then create your plan in Workspace.');toast('Model saved and selected.');
  }finally{$('save-model-button').disabled=false;}
});
modelProviderFields();
boot().catch(()=>{});
let pollTicks=0;
setInterval(()=>{if(state.me&&!document.hidden&&(state.pendingPlan||++pollTicks>=6)){pollTicks=0;refresh().catch(e=>toast(e.message,true));}},1000);
