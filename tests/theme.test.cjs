const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../isc_agents/platform/static/theme.js'),'utf8');

function browser(saved=null,dark=false,storageBlocked=false){
  const events={},documentEvents={},mediaEvents={},values=new Map(saved?[['isc-appearance',saved]]:[]);
  const selects=Array.from({length:3},()=>({value:'',events:{},addEventListener(name,fn){this.events[name]=fn;}}));
  const root={dataset:{}};
  const media={matches:dark,addEventListener(name,fn){mediaEvents[name]=fn;}};
  const window={matchMedia:()=>media,addEventListener:(name,fn)=>events[name]=fn};
  const document={documentElement:root,querySelectorAll:()=>selects,addEventListener:(name,fn)=>documentEvents[name]=fn};
  const localStorage={getItem:key=>{if(storageBlocked)throw Error('blocked');return values.get(key)||null;},setItem:(key,value)=>{if(storageBlocked)throw Error('blocked');values.set(key,value);}};
  vm.runInNewContext(source,{window,document,localStorage});
  documentEvents.DOMContentLoaded();
  return {root,selects,values,events,mediaEvents,media};
}

test('system appearance follows the OS and updates every selector',()=>{
  const b=browser(null,true);
  assert.equal(b.root.dataset.theme,'dark');
  assert.deepEqual(b.selects.map(s=>s.value),['system','system','system']);
  b.media.matches=false;b.mediaEvents.change();
  assert.equal(b.root.dataset.theme,'light');
});

test('explicit light choice persists and ignores subsequent OS dark changes',()=>{
  const b=browser('dark',true);
  b.selects[1].value='light';b.selects[1].events.change();
  assert.equal(b.root.dataset.theme,'light');
  assert.equal(b.values.get('isc-appearance'),'light');
  b.media.matches=true;b.mediaEvents.change();
  assert.equal(b.root.dataset.theme,'light');
  assert.deepEqual(b.selects.map(s=>s.value),['light','light','light']);
});

test('selecting system restores live OS tracking and cross-tab changes sync',()=>{
  const b=browser('light',true);
  b.selects[0].value='system';b.selects[0].events.change();
  assert.equal(b.root.dataset.theme,'dark');
  b.events.storage({key:'isc-appearance',newValue:'light'});
  assert.equal(b.root.dataset.theme,'light');
});

test('invalid or unavailable localStorage does not break appearance initialization',()=>{
  assert.equal(browser('invalid',true).root.dataset.theme,'dark');
  assert.equal(browser(null,false,true).root.dataset.theme,'light');
});
