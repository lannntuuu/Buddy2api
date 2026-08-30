export const api={
  async get(p,t){const h={};if(t)h.Authorization='Bearer '+t;const r=await fetch(p,{headers:h,credentials:'same-origin'});if(!r.ok)throw new Error(r.status);return r.json()},
  async post(p,b,t){const h={'Content-Type':'application/json'};if(t)h.Authorization='Bearer '+t;const r=await fetch(p,{method:'POST',headers:h,body:JSON.stringify(b),credentials:'same-origin'});if(!r.ok)throw new Error(r.status);return r.json()},
  async put(p,b,t){const h={'Content-Type':'application/json'};if(t)h.Authorization='Bearer '+t;const r=await fetch(p,{method:'PUT',headers:h,body:JSON.stringify(b),credentials:'same-origin'});if(!r.ok)throw new Error(r.status);return r.json()},
  async del(p,t){const h={};if(t)h.Authorization='Bearer '+t;const r=await fetch(p,{method:'DELETE',headers:h,credentials:'same-origin'});if(!r.ok)throw new Error(r.status);return r.json()},
};
export function apiErr(e,fallback='加载失败'){return e.message==='401'?'本机管理凭证无效，请刷新页面；远程访问时可在设置页填写备用 Token。':fallback}
export function n(v){return Number(v||0).toLocaleString()}
export function tok(v){v=Number(v||0);if(v>=1e9)return (v/1e9).toFixed(v>=1e10?1:2).replace(/\.?0+$/,'')+'B';if(v>=1e6)return (v/1e6).toFixed(v>=1e7?1:2).replace(/\.?0+$/,'')+'M';return v.toLocaleString()}
export function pct(v){return (Number(v||0)).toFixed(Number(v||0)%1?1:0)+'%'}
export function money(v){return Number(v||0).toFixed(4).replace(/\.?0+$/,'')}
export function ms(v){v=Number(v||0);return v>=1000?(v/1000).toFixed(1)+'s':v+'ms'}
export function fmt(t){return t?new Date(t*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'-'}
export function fmtSec(t){return t?new Date(t*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}):'-'}
