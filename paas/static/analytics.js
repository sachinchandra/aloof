/* Aloof analytics: PostHog US, cookieless. Loaded only when the server has a PostHog key (window.ALOOF_PH).
   No cookies; no persistent id. The anonymous id lives in sessionStorage for this tab only (cleared when the tab closes); first-touch UTM/referrer
   is kept in sessionStorage so the signup request can carry it. After login we identify by account id. */
(function(){var C=window.ALOOF_PH;if(!C||!C.key)return;if(C.hosts&&C.hosts.indexOf(location.hostname)<0)return;var H=(C.host||'https://us.i.posthog.com').replace(/\/$/,'');
var anon;try{anon=sessionStorage.getItem('aloof_anon')}catch(e){}if(!anon){anon='anon-'+Math.random().toString(36).slice(2)+Date.now().toString(36);try{sessionStorage.setItem('aloof_anon',anon)}catch(e){}}var uid=null;var T=false;try{if(/[?&](aloof_test|is_test)=(1|true)(&|$)/.test(location.search))sessionStorage.setItem('aloof_is_test','1');T=sessionStorage.getItem('aloof_is_test')==='1'}catch(e){}
function send(ev,p){var b={api_key:C.key,event:ev,distinct_id:uid||anon,properties:Object.assign({$current_url:location.href,$pathname:location.pathname,$host:location.host,$referrer:document.referrer||'$direct',$process_person_profile:!!uid,$lib:'aloof-web'},ft(),T?{is_test:true}:{},p||{}),timestamp:new Date().toISOString()};
 try{fetch(H+'/i/v0/e/',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b),keepalive:true,credentials:'omit'})}catch(e){}}
var K=['utm_source','utm_medium','utm_campaign','utm_term','utm_content'];
function ft(){try{return JSON.parse(sessionStorage.getItem('aloof_ft')||'{}')}catch(e){return{}}}
(function(){var s=new URLSearchParams(location.search),u={};K.forEach(function(k){if(s.get(k))u[k]=s.get(k).slice(0,200)});
 try{if(!sessionStorage.getItem('aloof_ft')){var r=document.referrer&&document.referrer.indexOf(location.origin)!==0?document.referrer:'';
  var f={};K.forEach(function(k){if(u[k])f['first_'+k]=u[k]});f.first_referrer=r.slice(0,500);var pc=(s.get('promo')||'').toUpperCase();if(/^[A-Z0-9]{3,20}$/.test(pc))f.first_promo=pc;f.first_landing=location.pathname;sessionStorage.setItem('aloof_ft',JSON.stringify(f))}}catch(e){}
 window.__aloof_utm=u})();
window.aloofAttribution=function(){var f=ft(),o={};Object.keys(f).forEach(function(k){o[k.replace(/^first_/,'')]=f[k]});return o};
window.aloofTrack=send;
function once(){var f=ft(),o={};K.forEach(function(k){if(f['first_'+k])o['initial_'+k]=f['first_'+k]});if(f.first_referrer)o.initial_referrer=f.first_referrer;if(f.first_landing)o.initial_landing=f.first_landing;return o}
window.aloofIdentify=function(id,email,isTest){if(!id)return;var prev=anon;uid=String(id);var st={is_test:!!isTest||T};send('$identify',{$anon_distinct_id:prev,$set:st,$set_once:once()})};
send('$pageview',Object.assign({},window.__aloof_utm,{referrer:document.referrer&&document.referrer.indexOf(location.origin)!==0?document.referrer.split('?')[0].slice(0,300):''}));
function esc(v){return String(v).replace(/\\/g,'\\\\').replace(/"/g,'\\"')}
function chain(el){var out=[],n=0;for(;el&&el.nodeType===1&&el.tagName!=='BODY'&&n<6;el=el.parentElement,n++){var t=el.tagName.toLowerCase(),c=(typeof el.className==='string'?el.className.trim().split(/\s+/).filter(Boolean).slice(0,5):[]),s=t+(c.length?'.'+c.join('.'):'');
 var tx=(t==='a'||t==='button')&&n===0?(el.innerText||'').trim().replace(/\s+/g,' ').slice(0,60):'';if(tx)s+=':text="'+esc(tx)+'"';
 var h=el.getAttribute('href');if(h)s+=(tx?'':':')+'attr__href="'+esc(h.split('?')[0].slice(0,200))+'"';if(el.id)s+='attr__id="'+esc(el.id)+'"';if(el.dataset&&el.dataset.cta)s+='attr__data-cta="'+esc(el.dataset.cta)+'"';out.push(s)}return out.join(';')}
document.addEventListener('click',function(e){var t=e.target;if(!t||!t.closest)return;var el=t.closest('a,button,[role=button],input[type=submit],input[type=button],summary,[data-cta]');if(!el)return;
 /* $autocapture: tag/classes/id/href (no query)/button text only; never input values */
 var tx=el.tagName==='A'||el.tagName==='BUTTON'?(el.innerText||'').trim().replace(/\s+/g,' ').slice(0,60):'';
 send('$autocapture',{$event_type:'click',$elements_chain:chain(el),$el_text:tx});
 var a=t.closest('[data-cta]');if(a)send('cta_clicked',{cta:a.dataset.cta,location:a.dataset.ctaLoc||location.pathname,href:(a.getAttribute('href')||'').split('?')[0].slice(0,200)});if(a&&a.dataset.cta==='subscribe_card'&&ft().first_promo)send('cta_clicked',{cta:ft().first_promo.toLowerCase()+'_claim',location:a.dataset.ctaLoc||location.pathname,promo:ft().first_promo})},true);
})();
