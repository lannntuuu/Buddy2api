// ── 跨页共享的格式化与剪贴板(spec 33 §2-E ④ 微去重)──────────────
// size/credit/age/expireMeta 原本在 channels/_login_import/dashboard/quota 各有一份,
// 逐字收敛到此处,不改任何输出格式;各页按需 import。
// copyText:写剪贴板并翻转调用方的 copied 状态。val 为布尔(keys/logs:闪烁
// true→false)或字符串(setup 页按名字点亮对应复制按钮,超时复位 '');
// ms 后复位;toast 由调用方自理(logs 页无 toast)。与四页原实现逐字等价。
export function copyText(v,mark,val,ms){navigator.clipboard.writeText(v);mark.value=val;setTimeout(()=>{mark.value=typeof val==='string'?'':false},ms)}
export function size(v){v=Number(v||0);if(v>=1024*1024)return(v/1024/1024).toFixed(1)+' MB';if(v>=1024)return(v/1024).toFixed(1)+' KB';return v+' B'}
export function credit(v){v=Number(v||0);return v.toLocaleString('zh-CN',{maximumFractionDigits:4})}
export function age(v){v=Number(v||0);if(v<60)return v+'s';if(v<3600)return Math.floor(v/60)+'m';return Math.floor(v/3600)+'h'}
export function expireMeta(a){if(a.next_expire_days===null||a.next_expire_days===undefined)return '无明确到期';return a.next_expire_days+' 天 · '+(a.next_expire_time||'-')}
