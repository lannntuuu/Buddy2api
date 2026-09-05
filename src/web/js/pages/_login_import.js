import {api,apiErr,fmt} from '../api.js';
import {I} from '../icons.js';
const{ref,reactive,computed,onMounted,watch}=Vue;

// 登录型通道「本机检测 → 文件表 → 一键导入」+ Trae SOLO 网页登录向导。
// 复用：通道管理页 Card C 选中通道的导入区 + 统一浮窗向导第二步共用此组件。
// 通过 props 传入 token/toast/通道 id；emit 'added' 让父级刷新凭证列表。
export default {
  props:['token','toast','channelId'],
  emits:['added'],
  setup(props,ctx){
    const chId=computed(()=>props.channelId||'');
    const disc=ref(null),dl=ref(false),scanning=ref(false),authPath=ref('');
    const solo=reactive({pending:false,url:'',pendingId:'',callbackUrl:'',state:'',uid:'',error:'',manual:''}),soloBusy=ref(false);
    let soloTimer=null,soloGen=0;
    // 统一出口:toast 走注入的函数,兜底 console
    function notify(m,t){const f=props.toast;if(typeof f==='function')f(m,t);else console.log('[login-import]',m)}
    function tk(){return props.token||''}

    async function discover(path=''){
      if(!chId.value){notify('请先选中一个通道','err');return}
      dl.value=true;
      try{
        const qs=new URLSearchParams();
        if(path&&path.trim())qs.set('auth_dir',path.trim());
        if(chId.value)qs.set('channel',chId.value);
        const q=qs.toString();
        disc.value=await api.get('/admin/accounts/discover'+(q?'?'+q:''),tk());
      }catch(e){notify(apiErr(e,'检测失败'),'err')}
      dl.value=false;
    }
    async function scan(path=''){
      if(scanning.value)return;
      scanning.value=true;
      try{
        let msg='导入完成';
        if(disc.value?.preview_token){
          const body={channel:disc.value.channel||'workbuddy',preview_token:disc.value.preview_token};
          if(path&&path.trim())body.auth_dir=path.trim();
          const r=await api.post('/admin/accounts/import',body,tk());
          msg='导入 '+r.imported+' · 更新 '+r.updated+' · 跳过 '+r.skipped;
        }else{
          const body=path&&path.trim()?{auth_dir:path.trim()}:{};
          const r=await api.post('/admin/accounts/scan',body,tk());
          msg='导入 '+r.imported+' · 更新 '+r.updated+' · 跳过 '+r.skipped;
        }
        notify(msg,'ok');
        ctx.emit('added');
        await discover(path);
      }catch(e){notify(apiErr(e,'扫描失败'),'err')}
      scanning.value=false;
    }
    function scanCustom(){const path=authPath.value.trim();if(!path){notify('请先填写目录或 .info 文件路径','err');return}scan(path)}
    function clearPath(){authPath.value='';discover('')}
    function size(v){v=Number(v||0);if(v>=1024*1024)return(v/1024/1024).toFixed(1)+' MB';if(v>=1024)return(v/1024).toFixed(1)+' KB';return v+' B'}

    function stopSoloPoll(){if(soloTimer){clearTimeout(soloTimer);soloTimer=null}}
    async function startSoloLogin(){
      if(soloBusy.value)return;
      stopSoloPoll();soloGen++;
      const gen=soloGen;soloBusy.value=true;
      try{
        const r=await api.post('/admin/traesolo/login/start',{},tk());
        if(gen!==soloGen)return;
        solo.pending=true;solo.url=r.login_url||'';solo.pendingId=r.pending_id||'';
        solo.callbackUrl=r.callback_url||'';solo.state='pending';solo.uid='';solo.error='';
        window.open(r.login_url,'_blank');pollSolo(gen);
      }catch(e){if(gen===soloGen){solo.pending=false;solo.error=''}notify(apiErr(e,'发起 SOLO 登录失败'),'err')}
      soloBusy.value=false;
    }
    async function pollSolo(gen){
      if(gen!==soloGen)return;if(!solo.pendingId)return;
      try{
        const r=await api.get('/admin/traesolo/login/result?pending_id='+encodeURIComponent(solo.pendingId),tk());
        if(gen!==soloGen)return;
        if(!r||r.found===false){stopSoloPoll();solo.pending=false;solo.state='expired';solo.error='登录会话已过期，可重新发起登录';return}
        solo.state=r.state||'';
        if(r.state==='success'){stopSoloPoll();solo.uid=r.uid||'';solo.pending=false;solo.error='';notify('SOLO 账号已添加'+(r.uid?'（'+r.uid+'）':''),'ok');ctx.emit('added');return}
        else if(r.state==='failed'){stopSoloPoll();solo.pending=false;solo.error=r.error||'登录失败';return}
        else if(r.state==='canceled'){stopSoloPoll();solo.pending=false;solo.error='';return}
        soloTimer=setTimeout(()=>pollSolo(gen),2500);
      }catch(e){if(gen!==soloGen)return;stopSoloPoll();solo.pending=false;if(e.message==='404'){solo.state='expired';solo.error='登录会话已过期，可重新发起登录'}else{solo.error='登录状态查询失败：'+apiErr(e);notify(solo.error,'err')}}
    }
    async function cancelSolo(){if(!solo.pendingId)return;stopSoloPoll();soloGen++;try{await api.post('/admin/traesolo/login/cancel',{pending_id:solo.pendingId},tk());solo.pending=false;solo.state='canceled';solo.error=''}catch(e){}}
    async function completeSolo(){
      const u=solo.manual.trim();if(!u){notify('请先粘贴完整回调 URL','err');return}
      soloBusy.value=true;
      try{
        const r=await api.post('/admin/traesolo/login/complete',{callback:u},tk());
        if(r.ok){notify('SOLO 账号已添加'+(r.uid?'（'+r.uid+'）':''),'ok');solo.manual='';ctx.emit('added')}
        else{notify(r.error||'导入失败','err')}
      }catch(e){notify(apiErr(e,'导入失败'),'err')}
      soloBusy.value=false;
    }

    onMounted(()=>{if(chId.value&&chId.value!=='traesolo')discover('')});
    watch(chId,(v,old)=>{if(!v||v===old)return;disc.value=null;authPath.value='';stopSoloPoll();Object.assign(solo,{pending:false,url:'',pendingId:'',state:'',uid:'',error:'',manual:''});if(v!=='traesolo')discover('')});

    return{disc,dl,scanning,authPath,discover,scan,scanCustom,clearPath,solo,soloBusy,startSoloLogin,cancelSolo,completeSolo,fmt,size,I}
  },
  template:`
<div v-if="channelId">
  <div v-if="channelId==='traesolo'" class="notebox">
    <h4 style="margin:0 0 8px">Web 登录（TRAE SOLO）</h4>
    <p class="hint" style="margin:0 0 10px">点击「发起网页登录」后在新窗口完成 TRAE 登录。本机部署时浏览器会自动跳回 <span class="mono">{{solo.callbackUrl||'/authorize'}}</span> 完成入库；远程部署够不到回调时，把浏览器地址栏里的完整回调 URL 粘到下方点「手动完成」。</p>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn s pri" @click="startSoloLogin" :disabled="soloBusy||solo.pending">{{solo.pending?'等待登录…':(solo.state==='success'?'重新发起登录':'发起网页登录')}}</button>
      <button class="btn s" @click="cancelSolo" :disabled="!solo.pending">取消</button>
      <span class="tag" v-if="solo.state==='success'">成功 · {{solo.uid}}</span>
      <span class="tag" v-else-if="solo.state==='failed'">失败</span>
      <a v-if="solo.url" :href="solo.url" target="_blank" class="hint" style="word-break:break-all">重新打开登录 URL ↗</a>
    </div>
    <div class="callout" v-if="solo.error" style="margin:10px 0 0">{{solo.error}}</div>
    <div style="display:flex;gap:8px;margin-top:10px">
      <input v-model="solo.manual" placeholder="手动闭环：粘贴完整回调 URL（http://…/authorize?refreshToken=…）"/>
      <button class="btn s" @click="completeSolo" :disabled="soloBusy">{{soloBusy?'处理中':'手动完成'}}</button>
    </div>
  </div>
  <div v-else class="notebox">
    <div class="detect-title" v-if="disc">检测到 {{disc.file_count}} 个登录文件，其中 {{disc.valid_count}} 个有效</div>
    <div class="detect-title" v-else-if="dl">正在检测本机登录文件</div>
    <div class="detect-title" v-else>本机登录检测（已导入账号默认只更新 token，不改权重/优先级）</div>
    <div class="hint">启动默认不再自动入库。已导入账号默认只更新 token，不改权重/优先级。</div>
    <div class="callout" v-if="disc?.runtime?.host_auth_limited" style="margin-bottom:12px">当前是 Linux Docker。QClaw / 千问办公的 Windows 登录文件在容器里解不开。这两家请在本机用 <span class="mono">python server.py</span> 启动后再检测导入。WorkBuddy 可以继续用这个容器。</div>
    <div class="callout" v-else-if="disc?.runtime?.container && !disc.runtime.auth_mount_exists" style="margin-bottom:12px">当前运行在 Docker 容器内，容器不能直接扫描 Windows 的 C 盘。Windows Docker 推荐用 <span class="mono">.\start-docker-win.ps1</span> 启动，它会自动把默认登录目录只读挂载到 <span class="mono">/auth</span>。</div>
    <div v-if="dl" class="load" style="padding:18px"><div class="spin"></div></div>
    <template v-else>
      <div class="detect-grid">
        <div class="detect-box">
          <h4>默认扫描目录</h4>
          <div v-if="disc?.dirs?.length">
            <div class="detect-path" v-for="d in disc.dirs" :key="d.path" :title="d.path">
              <span class="badge" :class="d.exists?(d.file_count?'ok':'inactive'):'err'">{{d.exists?d.file_count+' 个文件':'不存在'}}</span>
              <code :title="d.path">{{d.path}}</code>
            </div>
          </div>
          <div class="muted" v-else>暂无目录信息</div>
        </div>
        <div class="detect-box">
          <h4>自定义路径</h4>
          <div class="detect-custom">
            <input v-model="authPath" placeholder="auth 目录或 workbuddy-desktop.info 完整路径"/>
            <button class="btn s" @click="discover(authPath)" :disabled="dl">检测</button>
            <button class="btn s pri" @click="scanCustom" :disabled="dl||scanning">{{scanning?'导入中':'导入'}}</button>
            <button class="btn s" @click="clearPath" :disabled="dl">清空</button>
          </div>
          <div class="hint">可以直接粘贴 <span class="mono">C:/Users/.../auth</span> 或某个 <span class="mono">.info</span> 文件路径。</div>
        </div>
      </div>
      <div class="detect-files">
        <table class="mini-table" v-if="disc?.files?.length"><thead><tr><th>文件</th><th>状态</th><th>UID</th><th>域名</th><th>修改时间</th><th>大小</th><th>目录</th></tr></thead><tbody>
          <tr v-for="f in disc.files" :key="f.path"><td><div style="font-weight:600">{{f.account_name||f.name}}</div><div class="mono" :title="f.path">{{f.name}}</div></td><td><span class="badge" :class="f.valid?(f.already_imported?'inactive':'ok'):'err'">{{f.valid?(f.already_imported?'已导入':'可导入'):'无效'}}</span></td><td class="mono">{{f.uid_masked||'-'}}</td><td>{{f.domain||'-'}}</td><td class="mono">{{fmt(f.mtime)}}</td><td>{{size(f.size)}}</td><td class="mono" :title="f.dir">{{f.dir}}</td></tr>
        </tbody></table>
        <div class="empty" style="padding:18px" v-else>未检测到当前通道的登录文件</div>
      </div>
    </template>
  </div>
</div>
<div v-else class="hint">请先在「通道列表」选中一个登录型平台。</div>`};
