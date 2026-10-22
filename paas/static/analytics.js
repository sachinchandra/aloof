/* Aloof analytics: PostHog US, cookieless. Loaded only when the server has a PostHog key (window.ALOOF_PH).
   No cookies; no persistent id. The anonymous id lives in sessionStorage for this tab only (cleared when the tab closes); first-touch UTM/referrer
   is kept in sessionStorage so the signup request can carry it. After login we identify by account id. */
(function(){var C=window.ALOOF_PH;if(!C||!C.key)return;if(C.hosts&&C.hosts.indexOf(location.hostname)<0)return;var H=(C.host||'https://us.i.posthog.com').replace(/\/$/,'');
var anon;try{anon=sessionStorage.getItem('aloof_anon')}catch(e){}if(!anon){anon='anon-'+Math.random().toString(36).slice(2)+Date.now().toString(36);try{sessionStorage.setItem('aloof_anon',anon)}catch(e){}}var uid=null;
function send(ev,p){var b={api_key:C.key,event:ev,distinct_id:uid||anon,properties:Object.assign({$current_url:location.href,$pathname:location.pathname,$host:location.host,$referrer:document.referrer||'$direct',$process_person_profile:!!uid,$lib:'aloof-web'},ft(),p||{}),timestamp:new Date().toISOString()};
 try{fetch(H+'/i/v0/e/',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b),keepalive:true,credentials:'omit'})}catch(e){}}
var K=['utm_source','utm_medium','utm_campaign','utm_term','utm_content'];
function ft(){try{return JSON.parse(sessionStorage.getItem('aloof_ft')||'{}')}catch(e){return{}}}
(function(){var s=new URLSearchParams(location.search),u={};K.forEach(function(k){if(s.get(k))u[k]=s.get(k).slice(0,200)});
 try{if(!sessionStorage.getItem('aloof_ft')){var r=document.referrer&&document.referrer.indexOf(location.origin)!==0?document.referrer:'';
  var f={};K.forEach(function(k){if(u[k])f['first_'+k]=u[k]});f.first_referrer=r.slice(0,500);f.first_landing=location.pathname;sessionStorage.setItem('aloof_ft',JSON.stringify(f))}}catch(e){}
 window.__aloof_utm=u})();
window.aloofAttribution=function(){var f=ft(),o={};Object.keys(f).forEach(function(k){o[k.replace(/^first_/,'')]=f[k]});return o};
window.aloofTrack=send;
function once(){var f=ft(),o={};K.forEach(function(k){if(f['first_'+k])o['initial_'+k]=f['first_'+k]});if(f.first_referrer)o.initial_referrer=f.first_referrer;if(f.first_landing)o.initial_landing=f.first_landing;return o}
window.aloofIdentify=function(id,email,isTest){if(!id)return;var prev=anon;uid=String(id);var st={is_test:!!isTest};if(email)st.email=email;send('$identify',{$anon_distinct_id:prev,$set:st,$set_once:once()})};
send('$pageview',Object.assign({},window.__aloof_utm,{referrer:document.referrer&&document.referrer.indexOf(location.origin)!==0?document.referrer.split('?')[0].slice(0,300):''}));
document.addEventListener('click',function(e){var a=e.target.closest&&e.target.closest('[data-cta]');if(a)send('cta_click',{cta:a.dataset.cta,location:a.dataset.ctaLoc||location.pathname})},true);
})();
