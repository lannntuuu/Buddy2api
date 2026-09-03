import {I} from './icons.js';
import dash from './pages/dashboard.js';
import accs from './pages/accounts.js';
import quota from './pages/quota.js';
import keys from './pages/keys.js';
import chns from './pages/channels.js';
import usg from './pages/usage.js';
import lgs from './pages/logs.js';
import setup from './pages/setup.js';
import stgs from './pages/settings.js';

const{createApp,ref,onMounted}=Vue;

createApp({
  setup(){
    const validPages=['dashboard','accounts','quota','keys','channels','usage','logs','setup','settings'];
    const savedPage=localStorage.getItem('cb_gw_page');
    const page=ref(validPages.includes(savedPage)?savedPage:'dashboard'),token=ref(localStorage.getItem('cb_gw_token')||''),toasts=ref([]),meta=ref({title:'Buddy 2 API',version:''}),metaTag=ref('Local model gateway');
    onMounted(async()=>{try{const r=await fetch('/admin/meta',{credentials:'same-origin'});if(r.ok){const d=await r.json();meta.value={title:d.title||'Buddy 2 API',version:d.version||''}}}catch(_){meta.value={title:'Buddy 2 API',version:''}}});
    function tf(m,t='ok'){const id=Date.now()+Math.random();toasts.value=[...toasts.value,{id,m,t}].slice(-4);setTimeout(()=>toasts.value=toasts.value.filter(x=>x.id!==id),2500)}
    function go(k){page.value=k;localStorage.setItem('cb_gw_page',k)}
    function saveToken(value){token.value=value.trim();if(token.value)localStorage.setItem('cb_gw_token',token.value);else localStorage.removeItem('cb_gw_token');tf(token.value?'备用 Admin Token 已保存':'备用 Admin Token 已清除')}
    function hardRefresh(){window.location.reload()}
    const theme=ref(localStorage.getItem('cb_gw_theme')||'light');
    function toggleTheme(){theme.value=theme.value==='dark'?'light':'dark';document.documentElement.setAttribute('data-theme',theme.value);try{localStorage.setItem('cb_gw_theme',theme.value)}catch(_){}}
    const nav=[{k:'dashboard',l:'运行总览',i:I.dash},{k:'accounts',l:'账号管理',i:I.users},{k:'quota',l:'额度与积分',i:I.wallet},{k:'keys',l:'API Keys',i:I.key},{k:'channels',l:'通道与模型',i:I.cpu},{k:'usage',l:'用量统计',i:I.tokens},{k:'logs',l:'请求日志',i:I.log},{k:'setup',l:'接入指南',i:I.scan},{k:'settings',l:'设置',i:I.gear}];
    return{page,token,toasts,meta,metaTag,theme,toggleTheme,tf,go,saveToken,hardRefresh,nav,I}
  },
  template:`
  <div class="shell">
    <aside class="rail">
      <div class="rail-brand" v-html="I.logo"></div>
      <nav class="railnav">
        <div v-for="n in nav" :key="n.k" class="rail-item" :class="{on:page===n.k}" @click="go(n.k)" :title="n.l" v-html="n.i"></div>
      </nav>
      <div class="rail-foot">
        <button class="rail-icon" @click="toggleTheme" :title="theme==='dark'?'切到浅色':'切到深色'" v-html="theme==='dark'?I.sun:I.moon"></button>
      </div>
    </aside>
    <div class="shell-body">
      <div class="shell-head">
        <div class="shell-title">{{meta.title}}<span class="shell-ver" v-if="meta.version"> v{{meta.version}}</span></div>
        <div class="shell-actions">
          <span class="tag">{{metaTag}}</span>
          <button class="refresh-cta" @click="hardRefresh"><span v-html="I.refresh"></span><span>刷新</span></button>
        </div>
      </div>
      <main class="main">
        <div class="content" v-if="page==='dashboard'"><dash :token="token" :toast="tf"/></div>
        <div class="content" v-if="page==='accounts'"><accs :token="token" :toast="tf"/></div>
        <div class="content" v-if="page==='quota'"><quota :token="token" :toast="tf"/></div>
        <div class="content" v-if="page==='keys'"><keys :token="token" :toast="tf"/></div>
        <div class="content" v-if="page==='channels'"><chns :token="token" :toast="tf"/></div>
        <div class="content" v-if="page==='usage'"><usg :token="token" :toast="tf"/></div>
        <div class="content" v-if="page==='logs'"><lgs :token="token"/></div>
        <div class="content" v-if="page==='setup'"><setup :token="token" :toast="tf"/></div>
        <div class="content" v-if="page==='settings'"><stgs :token="token" :toast="tf" :save-token="saveToken"/></div>
      </main>
    </div>
    <div class="toasts"><div class="toast" :class="x.t" v-for="x in toasts" :key="x.id">{{x.m}}</div></div>
  </div>`
})
.component('dash',dash)
.component('accs',accs)
.component('quota',quota)
.component('keys',keys)
.component('chns',chns)
.component('usg',usg)
.component('lgs',lgs)
.component('stgs',stgs)
.component('setup',setup)
.mount('#app');
