// ── 错误文案映射(spec WS-3 §1)──────────────────────────────────
// 纯数据表:键=HTTP 状态(api 层 throw new Error(status)),值=用户可读文案,
// 一律带「下一步动作」。401/403 额外引导去「设置」页(app.js 的 toast 依
// toastActionFor 挂「去设置」跳转按钮)。便于静态断言(tests/test_web_assets.py)。
export const ERR_TEXTS={
  '400':'请求参数无效，请检查填写内容后重试。',
  '401':'管理凭证无效或已过期。本机访问刷新页面即可恢复；远程访问请到「设置」页保存备用 Admin Token。',
  '403':'没有操作权限。请确认使用的是管理员凭证，或到「设置」页更新备用 Token。',
  '404':'请求的资源不存在，可能已被删除，请刷新列表。',
  '409':'操作冲突，资源刚被其他操作修改，请刷新后重试。',
  '429':'请求过于频繁，请稍等几秒再试。',
  '500':'服务内部错误，请查看服务端日志后重试。',
  '502':'上游通道无有效响应，请到「通道管理」检查通道状态后重试。',
  '503':'服务暂不可用（可能正在重启或过载），请稍候重试；持续出现请检查服务进程是否存活。',
  '504':'请求超时，请检查服务是否存活、上游通道是否可达后重试。',
};
export function apiErr(e,fallback='加载失败'){
  const m=String((e&&e.message)||'');
  const base=ERR_TEXTS[m]||fallback;
  // _request 已从 4xx 响应体提取后端 detail(如 base_url 校验原因),拼在通用文案后,
  // 避免「请求参数无效」却看不到具体哪个字段错了。
  const d=(e&&e.detail)?String(e.detail):'';
  return d?base+' '+d:base;
}
// 401 文案 → toast 附带「去设置」跳转动作(app.js 消费);其余文案返回 null。
// 前缀匹配:apiErr 可能给 401 文案追加后端 detail,严格相等会丢动作。
export function toastActionFor(msg){return String(msg||'').startsWith(ERR_TEXTS['401'])?{label:'去设置',page:'settings'}:null}

// ── 契约 6:行对象回写 + 整表 load() 回退(spec WS-3 §3)─────────
// 写操作响应优先取行对象本地回写;缺行对象或形状不符返回 null,
// 调用方据此回退整表 load()。三个 helper 都是纯函数,便于静态断言。
export function respRow(r,field,test){
  if(!r||typeof r!=='object')return null;
  const row=r[field];
  if(!row||typeof row!=='object'||Array.isArray(row))return null;
  if(test&&!test(row))return null;
  return row;
}
export function respList(r,field){
  if(!r||typeof r!=='object')return null;
  const v=r[field];
  return Array.isArray(v)?v:null;
}
// 就地替换列表中同 id 行;合并保留行上未随响应返回的 UI 字段(如 channels.js
// 账号行的 _weight/_baseWeight 等)。找不到目标行返回 false(调用方回退整表)。
export function patchRowById(list,row,idKey='id'){
  if(!Array.isArray(list)||!row||row[idKey]===undefined||row[idKey]===null)return false;
  const i=list.findIndex(x=>x&&Number(x[idKey])===Number(row[idKey]));
  if(i<0)return false;
  list[i]={...list[i],...row};
  return true;
}

// ── 请求层:GET in-flight 去重 + 默认 15s 超时(spec WS-3 §4)────
// 同一 GET(token+URL 相同)并发期间只发一次,后续调用共享同一 Promise;
// 成败均从去重表移除,下一次点击仍会真正发请求。默认 15s 超时防悬挂,
// 慢端点(探活/批量/扫描)由调用方传 opt.timeoutMs 覆盖。
export const DEFAULT_TIMEOUT_MS=15000;
const INFLIGHT=new Map();
function _combineSignals(a,b){
  if(!a)return b;
  if(!b)return a;
  try{if(typeof AbortSignal.any==='function')return AbortSignal.any([a,b])}catch(_){}
  const c=new AbortController();
  const on=()=>c.abort();
  a.addEventListener('abort',on);
  b.addEventListener('abort',on);
  if(a.aborted||b.aborted)c.abort();
  return c.signal;
}
function _request(method,url,init,dedupeKey){
  if(dedupeKey&&INFLIGHT.has(dedupeKey))return INFLIGHT.get(dedupeKey);
  const ctrl=new AbortController();
  const timer=setTimeout(()=>ctrl.abort(),init.timeoutMs||DEFAULT_TIMEOUT_MS);
  const p=(async()=>{
    try{
      const r=await fetch(url,{method,headers:init.headers,body:init.body,credentials:'same-origin',signal:_combineSignals(init.signal,ctrl.signal)});
      if(!r.ok){
        let detail='';try{const j=await r.clone().json();if(j&&(typeof j.detail==='string'))detail=j.detail}catch(_){}
        const err=new Error(r.status);if(detail)err.detail=detail;throw err;
      }
      return await r.json();
    }catch(e){
      // 超时(内部 abort 而非外部 signal)统一映射成 504,走错误文案表
      if(ctrl.signal.aborted&&!(init.signal&&init.signal.aborted))throw new Error('504');
      throw e;
    }finally{clearTimeout(timer)}
  })();
  if(dedupeKey){
    INFLIGHT.set(dedupeKey,p);
    p.then(()=>INFLIGHT.delete(dedupeKey),()=>INFLIGHT.delete(dedupeKey));
  }
  return p;
}
export const api={
  get(p,t,opt){const h={};if(t)h.Authorization='Bearer '+t;
    return _request('GET',p,{headers:h,credentials:'same-origin',signal:opt&&opt.signal,timeoutMs:opt&&opt.timeoutMs},'GET\0'+(t||'')+'\0'+p)},
  post(p,b,t,opt){const h={'Content-Type':'application/json'};if(t)h.Authorization='Bearer '+t;
    return _request('POST',p,{headers:h,body:JSON.stringify(b),credentials:'same-origin',signal:opt&&opt.signal,timeoutMs:opt&&opt.timeoutMs})},
  put(p,b,t,opt){const h={'Content-Type':'application/json'};if(t)h.Authorization='Bearer '+t;
    return _request('PUT',p,{headers:h,body:JSON.stringify(b),credentials:'same-origin',signal:opt&&opt.signal,timeoutMs:opt&&opt.timeoutMs})},
  del(p,t,opt){const h={};if(t)h.Authorization='Bearer '+t;
    return _request('DELETE',p,{headers:h,credentials:'same-origin',signal:opt&&opt.signal,timeoutMs:opt&&opt.timeoutMs})},
};

// ── 展示格式化(保持既有行为)──────────────────────────────────
export function n(v){return Number(v||0).toLocaleString()}
export function tok(v){v=Number(v||0);if(v>=1e9)return (v/1e9).toFixed(v>=1e10?1:2).replace(/\.?0+$/,'')+'B';if(v>=1e6)return (v/1e6).toFixed(v>=1e7?1:2).replace(/\.?0+$/,'')+'M';return v.toLocaleString()}
export function pct(v){v=Number(v||0);return v.toFixed(v%1?2:0)+'%'}
export function money(v){return Number(v||0).toFixed(4).replace(/\.?0+$/,'')}
export function ms(v){v=Number(v||0);return v>=1000?(v/1000).toFixed(1)+'s':v+'ms'}
export function fmtTps(v){v=Number(v);return Number.isFinite(v)&&v>0?v.toFixed(1)+' t/s':'-'}
export function fmt(t){return t?new Date(t*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'-'}
export function fmtSec(t){return t?new Date(t*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}):'-'}

// ── busy 锁共享(channels/keys/quota 三页同构,spec 33 §2-E ④)──
// busy 是页面本地的 ref(键 `id+'-'+k`,页间不共享避免跨页脏读);
// withBusy 置位执行清位,busyKeyOf 供模板读。
export function busyKeyOf(busy,id,k){return busy.value[id+'-'+k]}
export async function withBusy(busy,id,k,fn){busy.value={...busy.value,[id+'-'+k]:true};try{return await fn()}finally{const o={...busy.value};delete o[id+'-'+k];busy.value=o}}
