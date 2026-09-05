import {api,apiErr,fmt,tok} from '../api.js';
import {I} from '../icons.js';
import LoginImport from './_login_import.js';
const{ref,reactive,computed,onMounted,watch,nextTick}=Vue;

export default {props:['token','toast'],components:{'login-import':LoginImport},setup(p){
  // ────────── master list ──────────
  const list=ref([]),ld=ref(true),err=ref(''),envLocked=ref(false),activeChannel=ref(''),toggling=ref({});
  async function loadList(){
    ld.value=true;err.value='';
    try{
      const r=await api.get('/admin/channels',p.token);
      list.value=(r.channels||[]).map(c=>({...c}));
      envLocked.value=!!r.env_locked;
      if(!list.value.some(c=>c.id===activeChannel.value))activeChannel.value=list.value.find(c=>c.enabled)?.id||list.value[0]?.id||'';
    }catch(e){err.value=apiErr(e,'加载通道列表失败')}
    ld.value=false;
  }
  function toggleChannel(id, on){
    if(envLocked.value){p.toast('CD env锁定中，无法更改（CB_GATEWAY_PROVIDERS）','err');return}
    const c=list.value.find(x=>x.id===id);if(c&&c.id!=='workbuddy')c.enabled=on;
    toggling.value={...toggling.value,[id]:true};
    api.put('/admin/channels',{enabled:list.value.filter(x=>x.enabled).map(x=>x.id),order:list.value.map(x=>x.id)},p.token).then(r=>{
      list.value.forEach(c=>{c.enabled=(r.enabled||[]).includes(c.id)});
    }).catch(e=>{p.toast(apiErr(e,'保存失败'),'err');loadList();}).finally(()=>{const o={...toggling.value};delete o[id];toggling.value=o});
  }
  const activeCh=computed(()=>list.value.find(c=>c.id===activeChannel.value));
  const loginChannels=computed(()=>list.value.filter(c=>c.kind!=='apikey'));
  const apikeyChannels=computed(()=>list.value.filter(c=>c.kind==='apikey'));

  // ────────── unified add/edit modal ──────────
  const um=ref({open:false,mode:'create',kind:'',channelId:'',tab:'form',infoId:'',infoKind:'',draft:{},warning:null,busy:false});
  function umEmptyDraft(){return {id:'',display_name:'',base_url:'',modelsText:'',aliases:'',env_api_key:'',api_key:''}}
  function openKeyModal(def){  // def: existing definition for edit; omit for create
    if(def){um.value={open:true,mode:'edit',kind:'apikey',channelId:def.id,tab:'form',infoId:'',infoKind:'',warning:null,busy:false,draft:{
      id:def.id,display_name:def.display_name||'',base_url:def.base_url||'',
      modelsText:(def.models||[]).join(', '),
      aliases:Object.entries(def.aliases||{}).map(([k,v])=>k+'→'+v).join('\n'),
      env_api_key:def.env_api_key||'',api_key:''}};}
    else{um.value={open:true,mode:'create',kind:'',channelId:'',tab:'form',infoId:'',infoKind:'',warning:null,busy:false,draft:umEmptyDraft()}}
  }
  function umClose(){um.value={open:false,mode:'create',kind:'',channelId:'',tab:'form',infoId:'',infoKind:'',warning:null,busy:false,draft:umEmptyDraft()}}
  // info tab: read-only summary opened from a row's 「详情」 button
  function openInfo(c){
    const isAk=c.kind==='apikey';
    um.value={open:true,tab:'info',mode:isAk&&ccOf(c.id)?'edit':'create',kind:isAk?'apikey':'login',channelId:c.id,infoId:c.id,infoKind:c.kind,warning:null,busy:false,draft:umEmptyDraft()};
  }
  // ────────── drag sort (SortableJS, global window.Sortable) ──────────
  const loginTbody=ref(null),apikeyTbody=ref(null),sortInst=[];
  function readOrder(tbody){return tbody?Array.from(tbody.querySelectorAll('tr[data-id]')).map(tr=>tr.dataset.id):[];}
  function applyOrder(ids){
    if(!ids.length)return;
    const map=new Map(list.value.map(c=>[c.id,c]));
    const ordered=ids.map(id=>map.get(id)).filter(Boolean);
    const rest=list.value.filter(c=>!ids.includes(c.id));
    list.value=ordered.concat(rest);
  }
  async function persistOrder(){
    const order=loginChannels.value.map(c=>c.id).concat(apikeyChannels.value.map(c=>c.id));
    const enabled=list.value.filter(c=>c.enabled).map(c=>c.id);
    try{const r=await api.put('/admin/channels',{enabled,order},p.token);list.value.forEach(c=>{c.enabled=(r.enabled||[]).includes(c.id)});}
    catch(e){p.toast(apiErr(e,'保存排序失败'),'err');await loadList();}
  }
  function onLoginEnd(){if(!loginTbody.value)return;applyOrder(readOrder(loginTbody.value));persistOrder();}
  function onApikeyEnd(){if(!apikeyTbody.value)return;applyOrder(readOrder(apikeyTbody.value));persistOrder();}
  async function umSave(){
    if(um.value.busy)return;
    const f=um.value.draft;
    if(!f.id.trim()||!f.display_name.trim()||!f.base_url.trim()){p.toast('id / 名称 / Base URL 必填','err');return}
    const models=f.modelsText.split(',').map(s=>s.trim()).filter(Boolean);
    if(!models.length){p.toast('至少填一个模型 id(逗号分隔)','err');return}
    const aliases={};f.aliases.split(/\r?\n/).forEach(line=>{const [k,...rest]=line.split('→');const v=rest.join('→');const ak=(k||'').trim(),av=(v||'').trim();if(ak&&av)aliases[ak]=av});
    const body={display_name:f.display_name.trim(),base_url:f.base_url.trim(),models,aliases};
    if(f.env_api_key.trim())body.env_api_key=f.env_api_key.trim();else body.env_api_key='';
    if(f.api_key.trim())body.api_key=f.api_key.trim();
    um.value.busy=true;
    try{
      if(um.value.mode==='create'){
        body.id=f.id.trim();
        const r=await api.post('/admin/channels/custom',body,p.token);
        if(r&&r.status==='saved_with_warning'&&r.warning){um.value.warning=r.warning;p.toast('已保存,但探活失败:HTTP '+r.warning.probe_status,'err')}
        else{p.toast('已添加 '+body.id);umClose()}
      }else{
        if(!f.api_key.trim())delete body.api_key;
        const r=await api.put('/admin/channels/custom/'+encodeURIComponent(f.id),body,p.token);
        if(r&&r.status==='saved_with_warning'&&r.warning){um.value.warning=r.warning;p.toast('已保存,但探活失败:HTTP '+r.warning.probe_status,'err')}
        else{p.toast('已更新 '+f.id);umClose()}
      }
      await Promise.all([loadCC(),loadList()]);
    }catch(e){p.toast(apiErr(e,'保存失败'),'err')}
    um.value.busy=false;
  }
  function onModalImported(){loadAccounts();}

  // ────────── definition: custom channel CRUD ──────────
  const ccList=ref([]),ccLd=ref(false),ccBusy=ref(false),ccErr=ref('');
  const ccForm=ref({mode:'create',draft:emptyCcDraft(),warning:null});
  function emptyCcDraft(){return {id:'',display_name:'',base_url:'',modelsText:'',aliases:'',env_api_key:'',api_key:''}}
  async function loadCC(){
    ccLd.value=true;ccErr.value='';
    try{const r=await api.get('/admin/channels/custom',p.token);ccList.value=r.channels||[]}
    catch(e){ccErr.value=apiErr(e,'加载自定义通道失败')}
    ccLd.value=false;
  }
  function ccOf(id){return ccList.value.find(x=>x.id===id)}
  // (ccStartCreate/ccStartEdit/ccSave 已迁入统一浮窗 um* 函数)
  async function ccDelete(c){
    if(!confirm('删除自定义通道 '+c.id+' ？该通道账号行将全部置 inactive。'))return;
    try{
      await api.del('/admin/channels/custom/'+encodeURIComponent(c.id),p.token);
      p.toast('已删除 '+c.id);
      await Promise.all([loadCC(),loadList()]);
    }catch(e){
      const m=String(e.message||'');
      p.toast(m==='409'?'seed 通道不允许删除，请用「启用通道」开关停用':'删除失败：'+apiErr(e),'err');
    }
  }

  // ────────── credential section: KEY_PANEL meta + discover/scan/solo ──────────
  // 合并自 accounts.js: KEY_PANEL_META = id → {name, base, env}，从 /admin/channels + /admin/channels/custom 推导
  const KEY_PANEL_META=ref({});
  // 模板用的是普通对象下标,这里给一个解包后的 computed,避免模板直接摸 .value
  const keyPanelMetaById=computed(()=>KEY_PANEL_META.value);
  function refreshKeyMeta(){
    const SEED_DEFAULT={gmi:{base:'https://api.gmi-serving.com/v1',env:'CB_GMI_API_KEY'},bailian:{base:'https://llm-7dqe434wikmhz0wa.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',env:'CB_BAILIAN_API_KEY'}};
    const meta={};
    for(const c of list.value){
      if(c.kind!=='apikey')continue;
      const seed=SEED_DEFAULT[c.id]||{};
      const custom=ccList.value.find(x=>x.id===c.id);
      meta[c.id]={name:c.display_name||c.id,base:(custom&&custom.base_url)||seed.base||'',env:(custom&&custom.env_api_key)||seed.env||''};
    }
    KEY_PANEL_META.value=meta;
  }
  const keyKey=ref(''),keyNick=ref(''),keyBase=ref(''),keyBusy=ref(false);
  const keyMode=computed(()=>{const c=activeCh.value;return c&&c.kind==='apikey'?c.id:''});
  const KEY_PANEL=computed(()=>KEY_PANEL_META.value[keyMode.value]||{name:'',base:'',env:''});

  const disc=ref(null),dl=ref(false),scanning=ref(false),authPath=ref('');
  const solo=reactive({pending:false,url:'',pendingId:'',callbackUrl:'',state:'',uid:'',error:'',manual:''}),soloBusy=ref(false);let soloTimer=null,soloGen=0;
  const soloSelected=computed(()=>activeChannel.value==='traesolo');
  async function discover(path=''){
    if(!activeChannel.value){p.toast('请先选中一个通道','err');return}
    dl.value=true;
    try{
      const qs=new URLSearchParams();
      if(path&&path.trim())qs.set('auth_dir',path.trim());
      if(activeChannel.value)qs.set('channel',activeChannel.value);
      const q=qs.toString();
      disc.value=await api.get('/admin/accounts/discover'+(q?'?'+q:''),p.token);
    }catch(e){p.toast(apiErr(e,'检测失败'),'err')}
    dl.value=false;
  }
  async function scan(path=''){
    if(scanning.value)return;
    scanning.value=true;
    try{
      if(disc.value?.preview_token){
        const body={channel:disc.value.channel||'workbuddy',preview_token:disc.value.preview_token};
        if(path&&path.trim())body.auth_dir=path.trim();
        const r=await api.post('/admin/accounts/import',body,p.token);
        p.toast('导入 '+r.imported+' · 更新 '+r.updated+' · 跳过 '+r.skipped);
      }else{
        const body=path&&path.trim()?{auth_dir:path.trim()}:{};
        const r=await api.post('/admin/accounts/scan',body,p.token);
        p.toast('导入 '+r.imported+' · 更新 '+r.updated+' · 跳过 '+r.skipped);
      }
      await loadAccounts();await discover(path);
    }catch(e){p.toast(apiErr(e,'扫描失败'),'err')}
    scanning.value=false;
  }
  async function scanCustom(){const path=authPath.value.trim();if(!path){p.toast('请先填写目录或 .info 文件路径','err');return}await scan(path)}
  function clearPath(){authPath.value='';discover('')}
  // (keyKey 粘贴逻辑已迁入统一浮窗 umSave;密钥型入口改为「添加/轮换密钥」按钮)
  function stopSoloPoll(){if(soloTimer){clearTimeout(soloTimer);soloTimer=null}}
  async function startSoloLogin(){
    if(soloBusy.value)return;
    stopSoloPoll();soloGen++;
    const gen=soloGen;soloBusy.value=true;
    try{
      const r=await api.post('/admin/traesolo/login/start',{},p.token);
      if(gen!==soloGen)return;
      solo.pending=true;solo.url=r.login_url||'';solo.pendingId=r.pending_id||'';
      solo.callbackUrl=r.callback_url||'';solo.state='pending';solo.uid='';solo.error='';
      window.open(r.login_url,'_blank');pollSolo(gen);
    }catch(e){if(gen===soloGen){solo.pending=false;solo.error=''}p.toast(apiErr(e,'发起 SOLO 登录失败'),'err')}
    soloBusy.value=false;
  }
  async function pollSolo(gen){
    if(gen!==soloGen)return;if(!solo.pendingId)return;
    try{
      const r=await api.get('/admin/traesolo/login/result?pending_id='+encodeURIComponent(solo.pendingId),p.token);
      if(gen!==soloGen)return;
      if(!r||r.found===false){stopSoloPoll();solo.pending=false;solo.state='expired';solo.error='登录会话已过期，可重新发起登录';return}
      solo.state=r.state||'';
      if(r.state==='success'){stopSoloPoll();solo.uid=r.uid||'';solo.pending=false;solo.error='';p.toast('SOLO 账号已添加'+(r.uid?'（'+r.uid+'）':''),'ok');await loadAccounts();await discover();return}
      else if(r.state==='failed'){stopSoloPoll();solo.pending=false;solo.error=r.error||'登录失败';return}
      else if(r.state==='canceled'){stopSoloPoll();solo.pending=false;solo.error='';return}
      soloTimer=setTimeout(()=>pollSolo(gen),2500);
    }catch(e){if(gen!==soloGen)return;stopSoloPoll();solo.pending=false;if(e.message==='404'){solo.state='expired';solo.error='登录会话已过期，可重新发起登录'}else{solo.error='登录状态查询失败：'+apiErr(e);p.toast(solo.error,'err')}}
  }
  async function cancelSolo(){if(!solo.pendingId)return;stopSoloPoll();soloGen++;try{await api.post('/admin/traesolo/login/cancel',{pending_id:solo.pendingId},p.token);solo.pending=false;solo.state='canceled';solo.error=''}catch(e){}}
  async function completeSolo(){
    const u=solo.manual.trim();if(!u){p.toast('请先粘贴完整回调 URL','err');return}
    soloBusy.value=true;
    try{
      const r=await api.post('/admin/traesolo/login/complete',{callback:u},p.token);
      if(r.ok){p.toast('SOLO 账号已添加'+(r.uid?'（'+r.uid+'）':''),'ok');solo.manual='';await loadAccounts();await discover()}
      else{p.toast(r.error||'导入失败','err')}
    }catch(e){p.toast(apiErr(e,'导入失败'),'err')}
    soloBusy.value=false;
  }

  // ────────── accounts list (full table from accounts.js) ──────────
  const accs=ref([]),accLd=ref(false),accBusy=ref({}),test=ref(null),tl=ref(0),filters=reactive({q:'',status:'all',sort:'priority',provider:'all'});
  function hydrate(a){return {...a,_weight:a.weight||1,_priority:a.priority||0,_creditSnapshot:a.credit_snapshot||a.credit_limit||0,_baseWeight:a.weight||1,_basePriority:a.priority||0,_baseCreditSnapshot:a.credit_snapshot||a.credit_limit||0}}
  function dirty(a){return Number(a._weight||1)!==Number(a._baseWeight||1)||Number(a._priority||0)!==Number(a._basePriority||0)||Number(a._creditSnapshot||0)!==Number(a._baseCreditSnapshot||0)}
  function busyKey(id,k){return accBusy.value[id+'-'+k]}
  async function loadAccounts(){
    accLd.value=true;
    try{accs.value=(await api.get('/admin/accounts',p.token)).map(hydrate)}
    catch(e){p.toast(apiErr(e),'err')}
    accLd.value=false;
  }
  function size(v){v=Number(v||0);if(v>=1024*1024)return(v/1024/1024).toFixed(1)+' MB';if(v>=1024)return(v/1024).toFixed(1)+' KB';return v+' B'}
  function credit(v){v=Number(v||0);return v.toLocaleString('zh-CN',{maximumFractionDigits:4})}
  function creditPct(a){return Math.max(0,Math.min(100,Number(a.credit_used_pct||0)))+'%'}
  function tokenLife(a){
    if(a.account_type==='api_key'||a.provider&&KEY_PANEL_META.value[a.provider])return '-';
    if(a.token_expired)return '过期';
    const h=Number(a.remaining_hours||0);if(h>=72)return '约 '+Math.floor(h/24)+' 天';if(h>=24)return Math.floor(h/24)+' 天 '+(h%24)+'h';return h+'h'
  }
  const visibleAccounts=computed(()=>{
    const q=filters.q.trim().toLowerCase();
    let rows=accs.value.filter(a=>{
      const hay=[a.nickname,a.name,a.uid,a.domain,a.status].join(' ').toLowerCase();
      if(q&&!hay.includes(q))return false;
      if(filters.status!=='all'&&a.status!==filters.status)return false;
      if(filters.provider!=='all'&&a.provider!==filters.provider)return false;
      return true;
    });
    rows=[...rows];
    rows.sort((a,b)=>{
      if(filters.sort==='used')return Number(b.total_credits||0)-Number(a.total_credits||0);
      if(filters.sort==='requests')return Number(b.total_requests||0)-Number(a.total_requests||0);
      return Number(b.priority||0)-Number(a.priority||0)||Number(b.weight||1)-Number(a.weight||1)||Number(a.total_requests||0)/Math.max(1,Number(a.weight||1))-Number(b.total_requests||0)/Math.max(1,Number(b.weight||1));
    });
    return rows;
  });
  async function withBusy(a,k,fn){accBusy.value={...accBusy.value,[a.id+'-'+k]:true};try{return await fn()}finally{const o={...accBusy.value};delete o[a.id+'-'+k];accBusy.value=o}}
  async function ref2(a){await withBusy(a,'refresh',async()=>{try{await api.post('/admin/accounts/'+a.id+'/refresh',{},p.token);p.toast('刷新成功');await loadAccounts()}catch(e){p.toast(apiErr(e,'刷新失败'),'err')}})}
  async function saveMeta(a){await withBusy(a,'save',async()=>{try{const creditSnapshot=Math.max(0,Number(a._creditSnapshot)||0);const body={weight:parseInt(a._weight)||1,priority:parseInt(a._priority)||0};if(Number(a._creditSnapshot||0)!==Number(a._baseCreditSnapshot||0))body.credit_limit=creditSnapshot;await api.put('/admin/accounts/'+a.id,body,p.token);a._baseWeight=parseInt(a._weight)||1;a._basePriority=parseInt(a._priority)||0;a._baseCreditSnapshot=creditSnapshot;p.toast(body.credit_limit!==undefined?'已保存余额快照':'已保存');await loadAccounts()}catch(e){p.toast(apiErr(e,'保存失败'),'err')}})}
  async function toggle(a){await withBusy(a,'toggle',async()=>{try{await api.put('/admin/accounts/'+a.id,{status:a.status==='active'?'inactive':'active'},p.token);p.toast(a.status==='active'?'已禁用':'已启用');await loadAccounts()}catch(e){p.toast(apiErr(e,'操作失败'),'err')}})}
  async function testOne(a){tl.value=a.id;test.value=null;try{const r=await api.post('/admin/accounts/'+a.id+'/test',{model:'auto',prompt:'ping'},p.token);test.value={account:a.nickname||a.name,result:r};p.toast(r.ok?'测试成功':'测试失败',r.ok?'ok':'err');await loadAccounts()}catch(e){p.toast(apiErr(e,'测试失败'),'err');test.value={account:a.nickname||a.name,result:{ok:false,status_code:0,message:e.message}}}tl.value=0}
  async function del(a){if(!confirm('删除账号 '+(a.nickname||a.name||a.id)+' ?'))return;await api.del('/admin/accounts/'+a.id,p.token);p.toast('已删除');await loadAccounts();await discover(authPath.value)}

  // ────────── 高级手动添加 (modal) ──────────
  const sa=ref(false),ai=ref(''),nm=ref(''),adding=ref(false);
  async function add(){
    if(adding.value)return;
    if(!ai.value.trim()){p.toast('请先粘贴 Auth JSON','err');return}
    let d;
    try{d=JSON.parse(ai.value)}catch(e){
      const raw=ai.value.trim();
      if(raw&&raw.length<4096&&!raw.startsWith('{')&&!raw.startsWith('['))d={api_key:raw};
      else{p.toast('失败:'+e.message,'err');adding.value=false;return}
    }
    adding.value=true;
    try{
      const r=await api.post('/admin/accounts',{...d,name:nm.value,provider:d.provider||d.channel||activeChannel.value},p.token);
      p.toast(r.updated?'账号已更新':'添加成功');sa.value=false;ai.value='';nm.value='';await loadAccounts();await discover(authPath.value);
    }catch(e){p.toast('失败:'+e.message,'err')}
    adding.value=false;
  }

  function initSortable(){
    if(sortInst.length||typeof window.Sortable==='undefined')return;
    const opt={handle:'.drag-handle',animation:120,onMove:e=>!e.related.classList.contains('grp-h')};
    if(loginTbody.value)sortInst.push(window.Sortable.create(loginTbody.value,{...opt,onEnd:onLoginEnd}));
    if(apikeyTbody.value)sortInst.push(window.Sortable.create(apikeyTbody.value,{...opt,onEnd:onApikeyEnd}));
  }
  onMounted(async()=>{
    await Promise.all([loadList(),loadCC(),loadAccounts()]);
    refreshKeyMeta();
    // 选中后默认做一次本机检测（登录型通道；密钥型自带面板）
    if(activeChannel.value&&activeChannel.value!=='traesolo')await discover('');
    await nextTick();initSortable();
  });
  watch(activeChannel,async(v,old)=>{
    if(!v)return;
    if(v===old)return;
    refreshKeyMeta();
    // 切通道时清 solo 状态(密钥粘贴已迁入浮窗)
    solo.pending=false;solo.url='';solo.pendingId='';solo.state='';solo.error='';solo.manual='';solo.uid='';
    stopSoloPoll();
    // 选中后默认做一次本机检测（登录型）
    if(v!=='traesolo')await discover('');
  });
  watch(list,()=>refreshKeyMeta(),{deep:false});
  watch(ccList,()=>refreshKeyMeta(),{deep:false});

  return{list,ld,err,envLocked,activeChannel,toggling,loadList,toggleChannel,activeCh,loginChannels,apikeyChannels,ccList,ccLd,ccBusy,ccErr,ccForm,ccOf,ccDelete,um,openKeyModal,umClose,umSave,onModalImported,openInfo,KEY_PANEL,keyMode,disc,dl,scanning,authPath,discover,scan,scanCustom,clearPath,solo,soloBusy,soloSelected,startSoloLogin,cancelSolo,completeSolo,accs,accLd,visibleAccounts,filters,busyKey,dirty,ref2,saveMeta,toggle,testOne,del,loadAccounts,size,credit,creditPct,tokenLife,test,tl,sa,ai,nm,adding,add,fmt,tok,I,keyPanelMetaById,loginTbody,apikeyTbody}
},template:`
<div>
  <div class="phead"><h1>通道管理</h1><p>定义通道 · 管理凭证 · 启用开关</p></div>

  <div class="card">
    <div class="card-h">通道列表<span class="sub">点行选中查看详情 · 开关即时生效</span></div>
    <div v-if="ld" class="load"><div class="spin"></div></div>
    <div v-else-if="err" style="padding:16px;color:var(--err);font-size:12px">{{err}}</div>
    <div v-else class="card-p" style="padding:0">
      <div v-if="envLocked" class="status-line ch-warn" style="margin:0;padding:10px 16px;border-bottom:1px solid var(--border)">
        检测到环境变量 <code>CB_GATEWAY_PROVIDERS</code>，通道开关为只读；如需在 UI 内调整，请去掉环境变量后重启。
      </div>
      <div class="table-scroll"><table>
        <tbody ref="loginTbody">
          <tr class="grp-h"><td colspan="5" style="background:var(--bg-sunken);font-weight:600;font-size:12px;color:var(--fg-2)">登录型平台<span class="hint" style="margin-left:8px;font-weight:400">硬编码配置模板 · 本机登录/网页登录导入 · 一个平台可挂多个凭证 · 行首 ≡ 拖拽排序</span></td></tr>
          <template v-for="c in loginChannels" :key="c.id">
          <tr @click="activeChannel=c.id" :class="{on:activeChannel===c.id}" style="cursor:pointer" :data-id="c.id">
            <td class="drag-handle" @click.stop style="width:18px;text-align:center;color:var(--fg-2);cursor:grab" title="拖拽排序">≡</td>
            <td>{{c.display_name||c.id}}</td>
            <td class="mono">{{c.id}}</td>
            <td><span class="tag">{{c.kind||'builtin'}}</span><span v-if="c.id==='workbuddy'" class="tag" style="margin-left:4px">必选</span><span v-else-if="!c.loaded" class="tag warn" style="margin-left:4px">未加载</span></td>
            <td style="text-align:right;white-space:nowrap"><button class="btn s" style="margin-right:8px" @click.stop="openInfo(c)">详情</button><input type="checkbox" :checked="c.enabled" :disabled="envLocked||c.id==='workbuddy'||toggling[c.id]" @change="toggleChannel(c.id,$event.target.checked)" @click.stop/></td>
          </tr>
          </template>
        </tbody>
        <tbody ref="apikeyTbody">
          <tr class="grp-h"><td colspan="5" style="background:var(--bg-sunken);font-weight:600;font-size:12px;color:var(--fg-2)">密钥型通道<span class="hint" style="margin-left:8px;font-weight:400">通用模板 · Base URL + API Key · 零代码新增 · 行首 ≡ 拖拽排序</span><button class="btn s pri" style="float:right;margin:2px 0" @click="openKeyModal()"><span v-html="I.plus"></span>新增</button></td></tr>
          <template v-for="c in apikeyChannels" :key="c.id">
          <tr @click="activeChannel=c.id" :class="{on:activeChannel===c.id}" style="cursor:pointer" :data-id="c.id">
            <td class="drag-handle" @click.stop style="width:18px;text-align:center;color:var(--fg-2);cursor:grab" title="拖拽排序">≡</td>
            <td>{{c.display_name||c.id}}</td>
            <td class="mono">{{c.id}}</td>
            <td><span class="tag apikey">{{c.kind||'apikey'}}</span><span v-if="c.custom" class="tag" style="margin-left:4px;background:var(--accent-soft);color:var(--accent)">custom</span><span v-else-if="c.source==='seed'||c.id==='gmi'||c.id==='bailian'" class="tag" style="margin-left:4px">seed</span><span v-else-if="!c.loaded" class="tag warn" style="margin-left:4px">未加载</span></td>
            <td style="text-align:right;white-space:nowrap"><button class="btn s" style="margin-right:8px" @click.stop="openInfo(c)">详情</button><input type="checkbox" :checked="c.enabled" :disabled="envLocked||toggling[c.id]" @change="toggleChannel(c.id,$event.target.checked)" @click.stop/></td>
          </tr>
          </template>
        </tbody>
      </table></div>
    </div>
  </div>

  <div v-if="activeCh" class="card" style="margin-top:16px">
    <div class="card-h">凭证 · <strong style="font-family:var(--mono)">{{activeCh.id}}</strong><span class="sub" style="margin-left:8px">{{activeCh.display_name||activeCh.id}}</span></div>
    <div class="card-p">

      <!-- ── credential section ── -->
      <div class="sec-h" style="margin-top:0">凭证</div>

      <!-- key (apikey): entry button opens unified modal -->
      <div v-if="keyMode" class="notebox">
        <div class="detect-title">{{KEY_PANEL.name}} · 上游密钥</div>
        <div class="hint">同一通道可保存多把密钥(不同 Key 各自成行、都参与调度),按权重/优先级轮换;不需要的行在下方列表停用即可。密钥通过浮窗添加或轮换,不会回显,列表只显示尾号。注意:这是上游通行证,与「API Keys」页发给客户端的网关 Key 是两回事。</div>
        <div style="display:flex;gap:8px;margin-top:8px">
          <button class="btn s pri" @click="openKeyModal()"><span v-html="I.plus"></span>添加/轮换密钥</button>
          <span class="hint" style="margin:0;align-self:center">环境变量 <span class="mono">{{KEY_PANEL.env||'(未配置)'}}</span> 仍可用(无可用密钥时自动导入)</span>
        </div>
      </div>

      <!-- login-type: reusable import component (also embedded in the unified modal) -->
      <login-import v-else-if="activeCh" :channel-id="activeChannel" @added="loadAccounts"></login-import>

      <!-- ── accounts list moved to standalone Card D below ── -->
    </div>
  </div>

  <!-- ── Card D: credentials list (global summary, independent of the selected channel) ── -->
  <div style="margin-top:16px">
  <div class="sec-h" style="margin-bottom:8px">凭证列表<span class="hint" style="margin-left:8px">全部通道的账号与密钥汇总;同一密钥型通道可并存多把 Key,按权重/优先级轮换</span></div>
  <div class="tbar"><button class="btn s" @click="loadAccounts" :disabled="accLd"><span v-html="I.refresh"></span>{{accLd?'刷新中':'刷新列表'}}</button><button class="btn s" @click="sa=true"><span v-html="I.plus"></span>高级手动添加</button><div class="spacer"></div><span class="tag" v-if="accs.length">{{visibleAccounts.length}}/{{accs.length}}个 · {{accs.filter(a=>a.status==='active').length}}活跃</span></div>
  <div v-if="accLd" class="load"><div class="spin"></div></div>
  <div class="card" v-else-if="accs.length"><div class="card-p" style="padding-bottom:0"><div class="control-row"><input class="searchbox" v-model="filters.q" placeholder="搜索账号 / UID / 域名"/><select class="selectctl" v-model="filters.status"><option value="all">全部状态</option><option value="active">active</option><option value="inactive">inactive</option><option value="expired">expired</option></select><select class="selectctl" v-model="filters.provider"><option value="all">全部通道</option><option v-for="c in list" :key="'fp-'+c.id" :value="c.id">{{c.display_name||c.id}}</option></select><select class="selectctl" v-model="filters.sort"><option value="priority">优先级 / 权重</option><option value="requests">请求数高到低</option><option value="used">累计已用高到低</option></select><div class="spacer"></div><span class="tag">当前 {{visibleAccounts.length}} 条</span></div></div><div class="table-scroll"><table><thead><tr><th>账号</th><th>通道</th><th>UID</th><th>状态</th><th>权重</th><th>优先级</th><th>余额快照</th><th>Token 有效期</th><th>请求</th><th>Token</th><th>累计已用</th><th></th></tr></thead><tbody>
    <tr v-for="a in visibleAccounts" :key="a.id"><td style="font-weight:600">{{a.nickname||a.name}} <span class="tag warn" v-if="dirty(a)">未保存</span></td><td><span class="tag">{{a.provider||'workbuddy'}}</span></td><td class="mono">{{a.uid?.slice(0,8)}}…</td><td><span class="badge" :class="a.status">{{a.status}}</span></td><td><input class="numctl" v-model.number="a._weight" type="number" min="1" max="100"/></td><td><input class="numctl" v-model.number="a._priority" type="number" min="-100" max="100"/></td><td class="credit-cell"><input class="numctl credit" v-model.number="a._creditSnapshot" type="number" min="0" step="0.01" placeholder="0"/><div class="credit-meta" v-if="a.credit_snapshot>0">余 {{credit(a.credit_remaining)}} · 已用 {{creditPct(a)}}</div><div class="credit-meta" v-else-if="a.total_credits>0">累计消耗(估算) {{credit(a.total_credits)}}</div><div class="credit-meta" v-else>官方失败时可手动校准</div></td><td>{{tokenLife(a)}}</td><td>{{a.total_requests}}</td><td>{{tok(a.total_tokens)}}</td><td>{{credit(a.total_credits)}}</td><td><div class="ops"><button class="btn s" @click="saveMeta(a)" :disabled="!dirty(a)||busyKey(a.id,'save')">{{busyKey(a.id,'save')?'保存中':'保存'}}</button><button class="btn s" @click="toggle(a)" :disabled="busyKey(a.id,'toggle')">{{busyKey(a.id,'toggle')?'处理中':(a.status==='active'?'禁用':'启用')}}</button><button class="btn s" @click="testOne(a)" :disabled="tl===a.id">{{tl===a.id?'测试中':'测试'}}</button><button class="btn s" @click="ref2(a)" :disabled="busyKey(a.id,'refresh')">{{busyKey(a.id,'refresh')?'刷新中':'刷新'}}</button><button class="btn s danger" @click="del(a)">删除</button></div></td></tr>
    <tr v-if="!visibleAccounts.length"><td colspan="12" class="empty">没有匹配的账号</td></tr>
  </tbody></table></div></div>
  <div class="card card-p empty" v-else><div class="em">🔌</div><p>暂无账号 · 登录型平台在上方「凭证」区域检测导入;密钥型通道通过浮窗添加密钥</p></div>
  </div>

  <!-- 统一新增/编辑浮窗 -->
  <div class="ov" v-if="um.open" @click.self="umClose()">
    <div class="modal" style="min-width:560px;max-width:720px">
      <div class="modal-h">
        <div>
          <h3 style="margin-bottom:6px">{{um.tab==='info'?'通道详情 · '+(um.infoId||'') : (um.mode==='create'?'新增通道凭证':'编辑密钥型通道 · '+um.draft.id)}}</h3>
          <div v-if="um.tab==='info'" class="tabbar"><button class="tab on">详情</button><button class="tab" :disabled="true">编辑</button></div>
        </div>
        <button class="x" @click="umClose()">&times;</button>
      </div>
      <div class="modal-b">
        <!-- info tab: read-only summary -->
        <template v-if="um.tab==='info'">
          <div v-if="um.infoKind==='apikey' && ccOf(um.infoId)" style="border:1px solid var(--border);border-radius:6px;padding:12px;background:var(--bg-elevated)">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;flex-wrap:wrap">
              <div>
                <div><span class="hint" style="margin:0">显示名</span> <strong>{{ccOf(um.infoId).display_name}}</strong></div>
                <div><span class="hint" style="margin:0">Base URL</span> <code class="mono">{{ccOf(um.infoId).base_url}}</code></div>
                <div><span class="hint" style="margin:0">模型</span> <span class="mono">{{(ccOf(um.infoId).models||[]).join(', ')}}</span></div>
                <div><span class="hint" style="margin:0">别名</span> <span class="mono">{{Object.entries(ccOf(um.infoId).aliases||{}).map(([k,v])=>k+'→'+v).join(', ')||'无'}}</span></div>
                <div><span class="hint" style="margin:0">环境变量</span> <span class="mono">{{ccOf(um.infoId).env_api_key||'-'}}</span></div>
                <div v-if="ccOf(um.infoId).source"><span class="hint" style="margin:0">来源</span> <span class="tag">{{ccOf(um.infoId).source}}</span></div>
              </div>
              <div style="display:flex;gap:6px;flex-shrink:0">
                <button class="btn s pri" @click="um.tab='form'"><span v-html="I.plus"></span>编辑</button>
                <button class="btn s danger" @click="ccDelete(ccOf(um.infoId))" v-if="ccOf(um.infoId).source!=='seed'" title="删除自定义通道">删除</button>
                <button class="btn s danger" v-else disabled title="seed 通道不允许删除，请用「启用通道」开关停用">删除(禁用)</button>
              </div>
            </div>
          </div>
          <div v-else-if="um.infoKind==='apikey'" style="border:1px solid var(--border);border-radius:6px;padding:12px;background:var(--bg-elevated)">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;flex-wrap:wrap">
              <div>
                <div><span class="hint" style="margin:0">类型</span> <strong>密钥型</strong></div>
                <div v-if="keyPanelMetaById[um.infoId]"><span class="hint" style="margin:0">Base URL</span> <code class="mono">{{keyPanelMetaById[um.infoId].base||'内置默认'}}</code></div>
                <div v-if="keyPanelMetaById[um.infoId]"><span class="hint" style="margin:0">环境变量</span> <span class="mono">{{keyPanelMetaById[um.infoId].env||'-'}}</span></div>
              </div>
              <div class="hint" style="margin:0">内置通道 · 定义不可编辑；启用 / 停用在上方开关</div>
            </div>
          </div>
          <div v-else style="border:1px solid var(--border);border-radius:6px;padding:12px;background:var(--bg-elevated)">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;flex-wrap:wrap">
              <div>
                <div><span class="hint" style="margin:0">类型</span> <strong>登录型</strong></div>
                <div><span class="hint" style="margin:0">导入方式</span> <span>本机登录 / 网页登录导入(一个平台可挂多个凭证)</span></div>
                <div v-if="keyPanelMetaById[um.infoId]"><span class="hint" style="margin:0">Base URL</span> <code class="mono">{{keyPanelMetaById[um.infoId].base||'内置默认'}}</code></div>
                <div v-if="keyPanelMetaById[um.infoId]"><span class="hint" style="margin:0">环境变量</span> <span class="mono">{{keyPanelMetaById[um.infoId].env||'-'}}</span></div>
                <div v-if="activeCh && activeCh.id===um.infoId && activeCh.checkin_supported"><span class="hint" style="margin:0">签到</span> <span class="tag">支持</span></div>
              </div>
              <div class="hint" style="margin:0">内置通道 · 定义不可编辑；启用 / 停用在上方开关</div>
            </div>
            <div class="notebox" style="margin-top:10px"><div class="hint">凭证在下方凭证区或浮窗向导中导入。</div></div>
          </div>
        </template>
        <!-- form tab (create/edit) -->
        <template v-else>
        <!-- step 1: choose type (create only) -->
        <template v-if="um.mode==='create' && !um.kind">
          <p class="hint" style="margin:0 0 10px">要添加哪一类?</p>
          <div style="display:flex;gap:10px">
            <button class="btn" style="flex:1;padding:14px" @click="um.kind='login'"><strong>登录型平台</strong><div class="hint" style="margin:4px 0 0">硬编码的五家平台 · 本机登录/网页登录导入</div></button>
            <button class="btn" style="flex:1;padding:14px" @click="um.kind='apikey'"><strong>密钥型通道</strong><div class="hint" style="margin:4px 0 0">通用模板 · Base URL + API Key 即可新增</div></button>
          </div>
        </template>
        <!-- step 2a: login-type wizard -->
        <template v-else-if="um.kind==='login'">
          <div class="field"><label>平台</label>
            <select class="selectctl" v-model="um.channelId">
              <option value="" disabled>选择平台</option>
              <option v-for="c in loginChannels" :key="c.id" :value="c.id">{{c.display_name||c.id}}</option>
            </select>
          </div>
          <login-import v-if="um.channelId" :channel-id="um.channelId" @added="onModalImported"></login-import>
        </template>
        <!-- step 2b: apikey form -->
        <template v-else-if="um.kind==='apikey'">
          <div class="form-grid">
            <div class="field"><label>通道 ID *<span class="hint" style="margin:0" v-if="um.mode==='create'">小写字母数字下划线连字符,32 字以内</span></label><input v-model="um.draft.id" :disabled="um.mode==='edit'" placeholder="如 siliconflow"/></div>
            <div class="field"><label>显示名称 *</label><input v-model="um.draft.display_name" placeholder="如 硅基流动"/></div>
            <div class="field"><label>Base URL *<span class="hint" style="margin:0">https:// 或 http://127.0.0.1[:port]</span></label><input v-model="um.draft.base_url" placeholder="https://api.example.com/v1"/></div>
            <div class="field"><label>模型白名单 *<span class="hint" style="margin:0">逗号分隔,至少一个;保存后可在「模型配置」页调整</span></label><input v-model="um.draft.modelsText" placeholder="model-a, model-b"/></div>
            <div class="field"><label>别名<span class="hint" style="margin:0">每行 "别名→模型 id",可空</span></label><textarea v-model="um.draft.aliases" rows="2" style="font-family:var(--mono)" placeholder="auto→model-a"></textarea></div>
            <div class="field"><label>环境变量名<span class="hint" style="margin:0">可选,匹配 ^CB_[A-Z0-9_]+$</span></label><input v-model="um.draft.env_api_key" placeholder="CB_MY_KEY"/></div>
            <div class="field"><label>API Key *<span class="hint" style="margin:0">{{um.mode==='edit'?'留空保留旧 Key;填则追加/轮换(同 Key 跳过)':'裸 Key / Bearer xxx / {"api_key":"..."} 均可'}}</span></label><input v-model="um.draft.api_key" type="password" :placeholder="um.mode==='edit'?'留空不轮换':'粘贴上游 API Key'"/></div>
          </div>
          <div v-if="um.warning" class="callout" style="margin-top:10px;font-size:12px;background:var(--warn-bg);border-color:var(--warn-border);color:var(--warn-fg)">探活失败(HTTP {{um.warning.probe_status}}):{{um.warning.probe_error||'无返回内容'}}。定义已保存,可稍后调整 Base URL 重试。</div>
        </template>
        </template>
      </div>
      <div class="modal-f" v-if="um.tab==='form'">
        <button class="btn" v-if="um.mode==='create'" @click="um.kind=''">上一步</button>
        <button class="btn" @click="umClose()">取消</button>
        <button class="btn pri" v-if="um.kind==='apikey'" @click="umSave" :disabled="um.busy">{{um.busy?'保存中…':(um.mode==='edit'?'保存修改':'创建通道并保存密钥')}}</button>
        <button class="btn pri" v-else-if="um.mode==='create'&&um.channelId" @click="umClose()">完成</button>
      </div>
    </div>
  </div>

  <!-- 高级手动添加 modal -->
  <div class="ov" v-if="sa" @click.self="sa=false"><div class="modal"><div class="modal-h"><h3>高级手动添加</h3><button class="x" @click="sa=false">&times;</button></div><div class="modal-b"><div class="field"><label>名称</label><input v-model="nm" placeholder="可选"/></div><div class="field"><label>Auth JSON 或 API Key</label><textarea v-model="ai" placeholder="粘贴 .info 文件内容；单 key 通道可直接粘 API Key / Bearer xxx"></textarea><div class="hint">WorkBuddy 等登录文件通道粘完整 JSON；密钥型通道可直接粘 API Key（自动识别）。</div></div></div><div class="modal-f"><button class="btn" @click="sa=false" :disabled="adding">取消</button><button class="btn pri" @click="add" :disabled="adding">{{adding?'添加中':'添加'}}</button></div></div></div>

  <!-- 测试结果 modal -->
  <div class="ov" v-if="test" @click.self="test=null"><div class="modal"><div class="modal-h"><h3>账号测试 · {{test.account}}</h3><button class="x" @click="test=null">&times;</button></div><div class="modal-b"><div class="testbox"><div class="row"><span>状态</span><span><span class="badge" :class="test.result.ok?'ok':'err'">{{test.result.ok?'成功':'失败'}}</span></span></div><div class="row"><span>HTTP</span><span class="mono">{{test.result.status_code}}</span></div><div class="row"><span>耗时</span><span class="mono">{{test.result.duration_ms}}ms</span></div><div class="row" v-if="test.result.model"><span>模型</span><span class="mono">{{test.result.model}}</span></div><div class="row" v-if="test.result.usage"><span>Token</span><span class="mono">{{tok(test.result.usage.total_tokens)}}</span></div><div class="msg">{{test.result.message||'无返回内容'}}</div></div><div class="hint" style="margin-top:8px">测试会发送一次极短请求，并记录到请求日志。</div></div><div class="modal-f"><button class="btn pri" @click="test=null">关闭</button></div></div></div>
</div>`};