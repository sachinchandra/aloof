/* Aloof analytics: PostHog EU, cookieless. Loaded only when the server has a PostHog key (window.ALOOF_PH).
   No cookies; no persistent id. The anonymous id lives in memory for the page; first-touch UTM/referrer
   is kept in sessionStorage so the signup request can carry it. After login we identify by account id. */
(function(){var C=window.ALOOF_PH;if(!C||!C.key)return;var H=(C.host||'https://eu.i.posthog.com').replace(/\/$/,'');
var anon='anon-'+Math.random().toString(36).slice(2)+Date.now().toString(36),uid=null;
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
window.aloofIdentify=function(id,email){if(!id)return;var prev=anon;uid=String(id);send('$identify',{$anon_distinct_id:prev})};
send('$pageview',window.__aloof_utm);
document.addEventListener('click',function(e){var a=e.target.closest&&e.target.closest('[data-cta]');if(a)send('cta_click',{cta:a.dataset.cta,location:a.dataset.ctaLoc||location.pathname})},true);
})();
