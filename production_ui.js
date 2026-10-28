// Static production read-only connection controls; no tokens are persisted.
const $=id=>document.getElementById(id);
const show=data=>{
 const panel=$('output');
 panel.textContent=typeof data==='string'?data:JSON.stringify(data,null,2);
 // Results are higher on this page than the account/balance controls.
 // Always bring the response into view when any button is clicked.
 panel.scrollIntoView({behavior:'smooth',block:'center'});
};
async function api(path,method='GET',body){
 const token=$('token').value.trim();
 if(!token)throw Error('Enter the ADMIN_TOKEN to access this page.');
 const r=await fetch('/production-readonly'+path,{method,cache:'no-store',headers:{'Authorization':'Bearer '+token,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined,credentials:'omit'});
 const data=await r.json().catch(()=>({error:'Non-JSON server response'}));
 if(!r.ok)throw Error(data.error||'Request failed.');
 return data;
}
function run(fn){return async()=>{try{show('Requesting...');await fn()}catch(e){show(e.message)}}}
$('status').onclick=run(async()=>show(await api('/status')));
$('start').onclick=run(async()=>{
 const d=await api('/connect/start','POST',{start:true});
 const url=new URL(d.authorize_url);
 if(url.protocol!=='https:'||url.hostname!=='us.etrade.com')throw Error('Unexpected E*TRADE authorization address');
 $('approval').href=url.toString();$('authorize').hidden=false;
 show('Open E*TRADE authorization link, approve access, then enter its verification code. This module only reads accounts.');
});
$('finish').onclick=run(async()=>{const d=await api('/connect/finish','POST',{verifier:$('verifier').value.trim()});$('verifier').value='';show(d)});
$('accounts').onclick=run(async()=>{
 const d=await api('/accounts');$('account').replaceChildren();
 const placeholder=new Option('Choose an account','');$('account').add(placeholder);
 for(const a of d.accounts||[]){if(a.accountIdKey){$('account').add(new Option((a.accountMode||'Unknown')+' · ending '+a.accountEnding,a.accountIdKey))}}
 show({...d,accounts:(d.accounts||[]).map(a=>({...a,accountIdKey:'[hidden in display]'}))});
});
function selected(){const id=$('account').value;if(!id)throw Error('Select an account first');return encodeURIComponent(id)}
$('balance').onclick=run(async()=>show(await api('/accounts/'+selected()+'/balance')));
$('classify').onclick=run(async()=>show(await api('/accounts/'+selected()+'/classification-check')));
$('positions').onclick=run(async()=>show(await api('/accounts/'+selected()+'/positions')));
$('calculate').onclick=run(async()=>{
 const params=new URLSearchParams({symbol:$('symbol').value.trim().toUpperCase(),limit_price:$('limit').value.trim(),budget:$('budget').value.trim()});
 const result=await api('/accounts/'+selected()+'/size-buy?'+params.toString());
 if(result.order){result.order={...result.order,account_id:'[hidden in display]'}}
 show(result);
});
show('Controls loaded. Enter your ADMIN_TOKEN and click Check status.');
