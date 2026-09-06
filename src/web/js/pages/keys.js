import {api,apiErr,tok,respRow,patchRowById} from '../api.js';
import {I} from '../icons.js';
const{ref,reactive,computed,onMounted}=Vue;

export default {props:['token','toast'],setup(p){
  const l=ref([]),ld=ref(true),sa=ref(false),f=reactive({name:'',models:'',limit:null,preset:'custom',channel:'workbuddy'}),res=ref(''),saving=ref(false),copied=ref(false),busy=ref({}),shown=ref({}),revealed=ref({}),channels=ref([]);
  const clientTypes=[
    {k:'custom',name:'默认',icon:'⚙'},
    {k:'codex',name:'Codex',icon:'◉'},
    {k:'opencode',name:'OpenCode',icon:'⬡'},
    {k:'openclaw',name:'OpenClaw',icon:'⬢'},
    {k:'cherry',name:'Cherry Studio',icon:'◉'},
    {k:'nextchat',name:'NextChat',icon:'◉'},
  ];
  function busyKey(id,k){return busy.value[id+'-'+k]}
  async function withBusy(k,id,fn){busy.value={...busy.value,[id+'-'+k]:true};try{return await fn()}finally{const o={...busy.value};delete o[id+'-'+k];busy.value=o}}
  async function load(){
    ld.value=true;
    try{
      l.value=await api.get('/admin/api-keys',p.token);
      // 通道下拉数据只在首次拉取,避免每次刷新列表都连带请求(spec WS-3 §3)
      if(!channels.value.length){try{const ch=await api.get('/admin/channels',p.token);if(ch.channels?.length)channels.value=ch.channels.filter(c=>c.enabled)}catch(e){}}
    }catch(e){p.toast(apiErr(e,'加载失败'),'err')}
    ld.value=false
  }
  async function create(){if(saving.value)return;if(!f.channel){p.toast('请选择通道','err');return}saving.value=true;try{const b={name:f.name||'Key',client_type:f.preset==='codex'?'codex':'custom',default_channel:f.channel};if(f.models)b.allowed_models=f.models.split(',').map(s=>s.trim()).filter(Boolean);if(f.limit)b.daily_limit=parseInt(f.limit);const r=await api.post('/admin/api-keys',b,p.token);res.value=r.key;p.toast('创建成功');await load()}catch(e){p.toast(apiErr(e,'创建失败'),'err')}saving.value=false}
  async function setChannel(k,channel){await withBusy('ch',k.id,async()=>{try{
    // 契约 2/6:PUT 返回 key 行对象 → 本地回写;缺行对象/形状不符 → 回退整表 load()
    const r=await api.put('/admin/api-keys/'+k.id,{default_channel:channel},p.token);
    const row=respRow(r,'key',x=>x&&x.id!=null);
    if(!(row&&patchRowById(l.value,row)))await load();
    p.toast('已切换通道');
  }catch(e){p.toast(apiErr(e,'切换失败'),'err')}})}
  async function toggle(k){await withBusy('toggle',k.id,async()=>{try{
    // 契约 2/6:同上,行对象本地回写,失败/形状不符回退整表
    const r=await api.put('/admin/api-keys/'+k.id,{status:k.status==='active'?'inactive':'active'},p.token);
    const row=respRow(r,'key',x=>x&&x.id!=null);
    if(!(row&&patchRowById(l.value,row)))await load();
    p.toast(k.status==='active'?'已禁用':'已启用');
  }catch(e){p.toast(apiErr(e,'操作失败'),'err')}})}
  async function del(k){if(!confirm('删除 API Key '+(k.name||k.key_prefix||k.id)+' ?'))return;await withBusy('del',k.id,async()=>{try{await api.del('/admin/api-keys/'+k.id,p.token);l.value=l.value.filter(x=>Number(x.id)!==Number(k.id));p.toast('已删除')}catch(e){p.toast(apiErr(e,'删除失败'),'err')}})}
  // ── 明文按需查看(spec WS-3 §7)──────────────────────────────
  // 列表只回 key_prefix(掩码);明文唯一来源是 GET /admin/api-keys/{id}/reveal,
  // 取回后暂存内存(revealed),「隐藏」只切显示不重新请求。
  async function fetchReveal(k){
    const r=await api.get('/admin/api-keys/'+k.id+'/reveal',p.token);
    if(r&&r.ok&&typeof r.key==='string'&&r.key)return r.key;
    throw new Error('500');
  }
  async function toggleReveal(k){
    if(shown.value[k.id]){shown.value={...shown.value,[k.id]:false};return}
    if(revealed.value[k.id]){shown.value={...shown.value,[k.id]:true};return}
    await withBusy('reveal',k.id,async()=>{
      try{const key=await fetchReveal(k);revealed.value={...revealed.value,[k.id]:key};shown.value={...shown.value,[k.id]:true}}
      catch(e){p.toast(e.detail||apiErr(e,'明文已不可恢复（旧版本只保存了哈希）'),'err')}
    });
  }
  async function cpKey(k){
    try{
      let key=revealed.value[k.id];
      if(!key){key=await fetchReveal(k);revealed.value={...revealed.value,[k.id]:key}}
      navigator.clipboard.writeText(key);copied.value=true;p.toast('已复制','info');setTimeout(()=>copied.value=false,1500);
    }catch(e){p.toast(e.detail||apiErr(e,'复制失败：明文已不可恢复'),'err')}
  }
  function cp(v){navigator.clipboard.writeText(v);copied.value=true;p.toast('已复制','info');setTimeout(()=>copied.value=false,1500)}
  function close(){sa.value=false;res.value='';f.name='';f.models='';f.limit=null;f.preset='custom';f.channel='workbuddy'}
  onMounted(load);return{l,ld,sa,f,res,saving,copied,busyKey,shown,revealed,clientTypes,channels,load,create,setChannel,toggle,del,cp,cpKey,toggleReveal,tok,close,I}
},template:`
<div>
  <div class="phead"><h1>API Keys</h1><p>客户端访问密钥</p></div>
  <div class="tbar"><button class="btn s pri" @click="sa=true"><span v-html="I.plus"></span>创建</button><button class="btn s" @click="load" :disabled="ld"><span v-html="I.refresh"></span>{{ld?'刷新中':'刷新'}}</button><div class="spacer"></div><span class="tag" v-if="l.length">{{l.length}}个</span></div>
  <div v-if="ld" class="load"><div class="spin"></div></div>
  <div class="card" v-else-if="l.length"><table><thead><tr><th>名称</th><th>类型</th><th>通道</th><th>Key</th><th>状态</th><th>模型</th><th>今日/限额</th><th>请求</th><th>Token</th><th></th></tr></thead><tbody>
    <tr v-for="k in l" :key="k.id"><td style="font-weight:600">{{k.name||'-'}}</td><td><span class="tag" :style="{background:k.client_type==='codex'?'var(--accent-soft)':'var(--bg-sunken)',color:k.client_type==='codex'?'var(--accent)':'var(--fg3)'}">{{k.client_type||'custom'}}</span></td><td><select class="selectctl" :value="k.default_channel||'workbuddy'" @change="setChannel(k,$event.target.value)" :disabled="busyKey(k.id,'ch')"><option v-for="c in channels" :key="c.id" :value="c.id">{{c.display_name||c.id}}</option></select></td><td><div style="display:flex;align-items:center;gap:5px;min-width:230px"><code style="flex:1;word-break:break-all">{{shown[k.id]&&revealed[k.id]?revealed[k.id]:(k.key_prefix||'sk-cb-…')}}</code><button class="btn s" @click="toggleReveal(k)" :disabled="busyKey(k.id,'reveal')">{{busyKey(k.id,'reveal')?'…':(shown[k.id]?'隐藏':'查看')}}</button><button class="btn s" @click="cpKey(k)" title="复制完整 API Key" :disabled="busyKey(k.id,'reveal')"><span v-html="I.copy"></span></button></div></td><td><span class="badge" :class="k.status">{{k.status}}</span></td><td>{{k.allowed_models?k.allowed_models.join(', '):'全部'}}</td><td>{{k.today_requests||0}} / {{k.daily_limit||'不限'}}</td><td>{{k.total_requests}}</td><td>{{tok(k.total_tokens)}}</td><td><button class="btn s" @click="toggle(k)" :disabled="busyKey(k.id,'toggle')" style="margin-right:4px">{{busyKey(k.id,'toggle')?'...':(k.status==='active'?'禁用':'启用')}}</button><button class="btn s danger" @click="del(k)" :disabled="busyKey(k.id,'del')">{{busyKey(k.id,'del')?'...':'删除'}}</button></td></tr>
  </tbody></table></div>
  <div class="card card-p empty" v-else><div class="em">🔑</div><p>暂无 Key</p></div>
  <div class="ov" v-if="sa" @click.self="close"><div class="modal" style="max-width:560px"><div class="modal-h"><h3>创建 Key</h3><button class="x" @click="close">&times;</button></div><div class="modal-b">
    <template v-if="res">
      <div class="field"><label>新 Key</label><div class="keybox">{{res}}</div><button class="btn s pri" style="margin-top:8px" @click="cp(res)">{{copied?'已复制':'复制 Key'}}</button><div class="hint" style="margin-top:6px">明文仅创建时完整展示一次；之后可在列表点「查看」按需取回。各客户端（Codex / OpenCode / Cherry Studio 等）的接入配置见「接入指南」页。</div></div>
      <div v-if="f.preset==='codex'" class="callout" style="margin-top:12px;font-size:12px;background:var(--ok-bg);border-color:var(--ok-border);color:var(--ok-fg)">
        <strong>已为该 Key 自动启用 Codex 专用处理</strong>：内容清洗（sandbox/filesystem/execute 等敏感词替换）、模型别名映射（gpt-5.5→glm-5.2 等）、工具过滤、Responses API 协议转换均自动生效，无需额外配置。一键写入 Codex 配置文件见「接入指南」页。
      </div>
    </template>
    <template v-else>
      <div class="field"><label>客户端类型</label>
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          <button v-for="t in clientTypes" :key="t.k" :class="['btn','s',{on:f.preset===t.k}]" @click="f.preset=t.k" style="border:1px solid var(--border);border-radius:var(--r);padding:6px 12px;font-size:12px;cursor:pointer" :style="{background:f.preset===t.k?'var(--accent-soft)':'var(--bg)',color:f.preset===t.k?'var(--accent)':'var(--fg)',borderColor:f.preset===t.k?'var(--accent-border)':'var(--border)',fontWeight:f.preset===t.k?'600':'400'}">{{t.icon}} {{t.name}}</button>
        </div>
        <div class="hint" style="margin-top:4px">选择目标客户端，创建后自动展示对应配置。选"默认"仅创建 Key（无任何改写，原样透传）。Codex 类型会对请求做敏感词清洗；其余类型仅展示对应客户端的接入配置模板，行为与"默认"完全一致。</div>
      </div>
      <div class="field"><label>通道</label><select class="selectctl" v-model="f.channel"><option v-for="c in channels" :key="c.id" :value="c.id">{{c.display_name||c.id}}</option></select><div class="hint">默认三个通道都能选。这把 Key 只会打到所选通道，不会静默切到另一家。可在列表里随时切换；进行中的请求不会改绑。</div></div>
      <div class="field"><label>名称</label><input v-model="f.name" placeholder="Key 名称"/></div>
      <div class="field"><label>允许模型</label><input v-model="f.models" placeholder="auto,glm-5.2（留空=全部）"/></div>
      <div class="field"><label>每日请求限额</label><input v-model="f.limit" type="number" placeholder="不限"/></div>
    </template>
  </div><div class="modal-f"><button class="btn" @click="close">{{res?'关闭':'取消'}}</button><button class="btn pri" @click="create" v-if="!res" :disabled="saving">{{saving?'创建中':'创建'}}</button></div></div></div>
</div>`};
