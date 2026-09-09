import {I} from './icons.js';
import {toastActionFor} from './api.js';
import dash from './pages/dashboard.js';
import quota from './pages/quota.js';
import keys from './pages/keys.js';
import chns from './pages/channels.js';
import mdls from './pages/models.js';
import usg from './pages/usage.js';
import lgs from './pages/logs.js';
import setup from './pages/setup.js';
import stgs from './pages/settings.js';

const{createApp,ref,onMounted}=Vue;

// toast 时长(spec WS-3 §1):错误长驻 8s + 关闭按钮;成功保持 2.5s
const TOAST_ERR_MS=8000,TOAST_OK_MS=2500;

createApp({
  setup(){
    const validPages=['dashboard','channels','models','keys','usage','logs','quota','setup','settings'];
    // hash 路由(spec WS-3 §6):启动 hash 优先(#/keys),空 hash 回退
    // localStorage 记忆;go() 同步 location.hash,hashchange 驱动渲染。
    function pageFromHash(){const h=(location.hash||'').replace(/^#\/?/,'');return validPages.includes(h)?h:null}
    // 页面记忆:新键 cb_gw_page_v2 存当前页;旧键 cb_gw_page 只做一次性迁移
    // (accounts→channels、legacy channels(旧「通道与模型」)→models)。
    // 不能直接重读旧键:channels 现在是合法页面值,否则每次刷新都会把「通道管理」错误映射到「模型配置」。
    let migrated=null;
    try{migrated=localStorage.getItem('cb_gw_page_v2')}catch(_){}
    if(!migrated){
      let savedRaw=null;
      try{savedRaw=localStorage.getItem('cb_gw_page')}catch(_){}
      migrated=savedRaw==='accounts'?'channels':(savedRaw==='channels'?'models':savedRaw);
    }
    try{localStorage.setItem('cb_gw_page_v2',migrated||'dashboard')}catch(_){}
    const initial=pageFromHash()||(validPages.includes(migrated)?migrated:'dashboard');
    const page=ref(initial),token=ref(localStorage.getItem('cb_gw_token')||''),toasts=ref([]),meta=ref({title:'Buddy 2 API',version:''}),metaTag=ref('Local model gateway');
    onMounted(async()=>{try{const r=await fetch('/admin/meta',{credentials:'same-origin'});if(r.ok){const d=await r.json();meta.value={title:d.title||'Buddy 2 API',version:d.version||''}}}catch(_){meta.value={title:'Buddy 2 API',version:''}}});
    function dismiss(id){toasts.value=toasts.value.filter(x=>x.id!==id)}
    function tf(m,t='ok'){
      const id=Date.now()+Math.random();
      const err=t==='err';
      // 401 文案附带「去设置」跳转动作(api.js toastActionFor)
      const item={id,m,t,err,action:err?toastActionFor(m):null};
      toasts.value=[...toasts.value,item].slice(-4);
      setTimeout(()=>dismiss(id),err?TOAST_ERR_MS:TOAST_OK_MS);
    }
    function toastGo(x){if(!x||!x.action)return;dismiss(x.id);go(x.action.page)}
    // 跨页共享的通道下拉数据:各页形状不同,拉取与缓存统一在这里(INFLIGHT 去重)。
    // 返回值即通道数组(失败返回 []),调用方只管 await,不读 sharedChannels ref。
    const sharedChannels=ref(null);
    let sharedChannelsInflight=null;
    async function ensureChannels(token){
      if(sharedChannels.value)return sharedChannels.value;
      if(!sharedChannelsInflight){
        sharedChannelsInflight=(async()=>{
          try{const ch=await api.get('/admin/channels',token);sharedChannels.value=ch.channels||[]}
          catch(_){sharedChannels.value=[]}
          finally{sharedChannelsInflight=null}
          return sharedChannels.value;
        })();
      }
      return sharedChannelsInflight;
    }
    function invalidateChannels(){sharedChannels.value=null}
    function syncHash(k){const nh='#/'+k;if((location.hash||'')!==nh){try{location.hash=nh}catch(_){}}}
    function go(k){page.value=k;try{localStorage.setItem('cb_gw_page_v2',k)}catch(_){}syncHash(k)}
    window.addEventListener('hashchange',()=>{
      const k=pageFromHash();
      if(!k||k===page.value)return;
      page.value=k;
      try{localStorage.setItem('cb_gw_page_v2',k)}catch(_){}
    });
    function saveToken(value){token.value=value.trim();if(token.value)localStorage.setItem('cb_gw_token',token.value);else localStorage.removeItem('cb_gw_token');tf(token.value?'备用 Admin Token 已保存':'备用 Admin Token 已清除')}
    function hardRefresh(){window.location.reload()}
    const theme=ref(localStorage.getItem('cb_gw_theme')||'light');
    function toggleTheme(){theme.value=theme.value==='dark'?'light':'dark';document.documentElement.setAttribute('data-theme',theme.value);try{localStorage.setItem('cb_gw_theme',theme.value)}catch(_){}}
    const nav=[{k:'dashboard',l:'运行总览',i:I.dash},{k:'channels',l:'通道管理',i:I.cpu},{k:'models',l:'模型配置',i:I.tokens},{k:'keys',l:'API Keys',i:I.key},{k:'usage',l:'用量统计',i:I.tokens},{k:'logs',l:'请求日志',i:I.log},{k:'quota',l:'额度与积分',i:I.wallet},{k:'setup',l:'接入指南',i:I.scan},{k:'settings',l:'设置',i:I.gear}];
    const railOpen=ref(localStorage.getItem('cb_gw_rail')==='expanded');
    function toggleRail(){railOpen.value=!railOpen.value;try{localStorage.setItem('cb_gw_rail',railOpen.value?'expanded':'collapsed')}catch(_){}}
    return{page,token,toasts,meta,metaTag,theme,toggleTheme,tf,dismiss,toastGo,go,saveToken,hardRefresh,nav,railOpen,toggleRail,I,sharedChannels,ensureChannels,invalidateChannels}
  },
  template:`
  <div class="shell">
    <aside class="rail" :class="{open:railOpen}">
      <div class="rail-brand">
        <span class="rail-brand-ic" v-html="I.logo"></span>
        <span class="rail-brand-txt" v-if="railOpen">
          <span class="rail-brand-name">{{meta.title}}</span>
          <span class="rail-brand-ver" v-if="meta.version">v{{meta.version}}</span>
        </span>
      </div>
      <nav class="railnav">
        <div v-for="n in nav" :key="n.k" class="rail-item" :class="{on:page===n.k}" role="button" tabindex="0" @click="go(n.k)" @keydown.enter="go(n.k)" @keydown.space.prevent="go(n.k)" :aria-current="page===n.k?'page':false" :title="n.l">
          <span class="rail-ic" v-html="n.i"></span><span class="rail-lbl" v-if="railOpen">{{n.l}}</span>
        </div>
      </nav>
      <div class="rail-foot">
        <button class="rail-icon" @click="toggleRail" :title="railOpen?'收起侧栏':'展开侧栏'" v-html="railOpen?I.chevronL:I.chevronR"></button>
        <button class="rail-icon" @click="toggleTheme" :title="theme==='dark'?'切到深色':'切到浅色'" v-html="theme==='dark'?I.moon:I.sun"></button>
      </div>
    </aside>
    <div class="shell-body">
      <div class="shell-head">
        <div class="shell-actions">
          <span class="tag">{{metaTag}}</span>
          <button class="refresh-cta" @click="hardRefresh"><span v-html="I.refresh"></span><span>刷新</span></button>
        </div>
      </div>
      <main class="main">
        <div class="content" v-if="page==='dashboard'"><dash :token="token" :toast="tf"/></div>
        <div class="content" v-if="page==='channels'"><chns :token="token" :toast="tf" :invalidate-channels="invalidateChannels"/></div>
        <div class="content" v-if="page==='models'"><mdls :token="token" :toast="tf"/></div>
        <div class="content" v-if="page==='quota'"><quota :token="token" :toast="tf"/></div>
        <div class="content" v-if="page==='keys'"><keys :token="token" :toast="tf" :ensure-channels="ensureChannels"/></div>
        <div class="content" v-if="page==='usage'"><usg :token="token" :toast="tf" :ensure-channels="ensureChannels"/></div>
        <div class="content" v-if="page==='logs'"><lgs :token="token"/></div>
        <div class="content" v-if="page==='setup'"><setup :token="token" :toast="tf"/></div>
        <div class="content" v-if="page==='settings'"><stgs :token="token" :toast="tf" :save-token="saveToken"/></div>
      </main>
    </div>
    <div class="toasts" role="status" aria-live="polite"><div class="toast" :class="x.t" v-for="x in toasts" :key="x.id"><span class="toast-msg">{{x.m}}</span><button v-if="x.action" class="toast-act" @click="toastGo(x)">{{x.action.label}}</button><button v-if="x.err" class="toast-x" @click="dismiss(x.id)" aria-label="关闭提示">&times;</button></div></div>
  </div>`
})
.component('dash',dash)
.component('quota',quota)
.component('keys',keys)
.component('chns',chns)
.component('mdls',mdls)
.component('usg',usg)
.component('lgs',lgs)
.component('stgs',stgs)
.component('setup',setup)
.mount('#app');
