import {api,apiErr,fmt} from '../api.js';
import {I} from '../icons.js';
const{ref,reactive,computed,onMounted}=Vue;

export default {props:['token','toast'],setup(p){
  const l=ref([]),creditSum=ref(null),ld=ref(true),cld=ref(false),checkins=ref({}),checkinSummary=ref(null),checkinLoading=ref(false),claimingAll=ref(false),officialRefreshing=ref(false),pkg=ref(null),claim=ref(null),busy=ref({});
  function busyKey(id,k){return busy.value[id+'-'+k]}
  function withBusy(id,k,fn){busy.value={...busy.value,[id+'-'+k]:true};try{return fn()}finally{const o={...busy.value};delete o[id+'-'+k];busy.value=o}}
  function chName(a){return a.provider||'workbuddy'}
  async function load(){ld.value=true;try{l.value=await api.get('/admin/accounts',p.token)}catch(e){p.toast(apiErr(e),'err')}ld.value=false;await Promise.all([loadCheckins(false),loadCredit(false)])}
  async function loadCredit(force){try{creditSum.value=await api.get('/admin/credit-summary'+(force?'?force=1':''),p.token)}catch(e){if(force)p.toast(apiErr(e,'官方额度加载失败'),'err')}}
  async function loadCheckins(force=false){checkinLoading.value=true;try{const r=await api.get('/admin/accounts/checkin-status-all'+(force?'?force=1':''),p.token);checkinSummary.value=r;const map={};(r.results||[]).forEach(x=>{map[x.account_id]=x});checkins.value=map}catch(e){if(force)p.toast(apiErr(e,'签到状态加载失败'),'err')}checkinLoading.value=false}
  async function refreshAllResources(){if(officialRefreshing.value)return;officialRefreshing.value=true;const targets=l.value.filter(a=>a.status==='active');await Promise.all(targets.map(a=>refreshResource(a,true,true)));officialRefreshing.value=false;p.toast('官方额度已刷新 '+targets.length+' 个账号','ok')}
  async function refreshResource(a,silent=false,force=false){return await withBusy(a.id,'resource',async()=>{try{const r=await api.get('/admin/accounts/'+a.id+'/resources'+(force?'?force=1':''),p.token);a.official_resource=r;if(!silent)p.toast(r.ok?(r.stale?'已显示旧额度缓存':'官方额度已刷新'):('官方额度失败：'+(r.message||r.status_code)),r.ok?'ok':'err');return r}catch(e){const r={ok:false,account_id:a.id,message:apiErr(e,'官方额度加载失败'),packages:[]};a.official_resource=r;if(!silent)p.toast(r.message,'err');return r}})}
  async function openPackages(a){if(!a.official_resource&&!busyKey(a.id,'resource'))await refreshResource(a,true);pkg.value={account:a,resource:a.official_resource||{ok:false,message:'未获取官方额度',packages:[]}}}
  async function refreshPackages(){if(!pkg.value)return;const r=await refreshResource(pkg.value.account,true);pkg.value={account:pkg.value.account,resource:r||pkg.value.account.official_resource||pkg.value.resource}}
  async function claimOne(a){await withBusy(a.id,'claim',async()=>{try{const r=await api.post('/admin/accounts/'+a.id+'/checkin',{},p.token);checkins.value={...checkins.value,[a.id]:r};claim.value={title:'领取结果',results:[r]};p.toast(r.claimed?'领取成功':(r.already_claimed?'今日已领':'领取失败'),r.ok?'ok':'err');await loadCredit(true);await loadCheckins(true)}catch(e){p.toast(apiErr(e,'领取失败'),'err')}})}
  async function claimAll(){if(claimingAll.value)return;claimingAll.value=true;try{const r=await api.post('/admin/accounts/checkin-all',{},p.token);claim.value={title:'一键领取结果',summary:r,results:r.results||[]};p.toast('领取 '+r.claimed+' · 已领 '+r.already_claimed+' · 失败 '+r.failed,r.failed?'err':'ok');await load();await loadCredit(true)}catch(e){p.toast(apiErr(e,'一键领取失败'),'err')}claimingAll.value=false}
  function credit(v){v=Number(v||0);return v.toLocaleString('zh-CN',{maximumFractionDigits:4})}
  function age(v){v=Number(v||0);if(v<60)return v+'s';if(v<3600)return Math.floor(v/60)+'m';return Math.floor(v/3600)+'h'}
  function officialBalance(a){const r=a.official_resource;if(!r||!r.ok||r.unsupported)return null;if((a.provider||'workbuddy')!=='workbuddy'&&r.unit&&r.unit!=='credit')return null;const v=r.total_dosage??r.available_total??r.remaining;if(v==null||v==='')return null;return Number(v)}
  function officialMeta(a){const r=a.official_resource;if(!r)return '未刷新';if(r.unsupported)return '无积分接口';if(!r.ok)return r.message||'加载失败';if((a.provider||'workbuddy')!=='workbuddy')return '积分';return '30 天内到期 '+credit(r.expiring_30d_total)+' · '+(r.package_count||0)+' 包'+(r.stale?' · 旧缓存':'')}
  function cacheAge(a){const r=a.official_resource;if(!r)return '';const v=Number(r.age_seconds||0);if(v<60)return v+'s 前';if(v<3600)return Math.floor(v/60)+'m 前';return Math.floor(v/3600)+'h 前'}
  function officialWarn(a){return Number(a.official_resource?.expiring_30d_total||0)>0}
  function expireMeta(a){if(a.next_expire_days===null||a.next_expire_days===undefined)return '无明确到期';return a.next_expire_days+' 天 · '+(a.next_expire_time||'-')}
  function checkinOf(a){return checkins.value[a.id]||null}
  function checkinClass(a){const r=checkinOf(a);if(!r)return 'inactive';if(!r.ok)return 'err';if(r.claimed||r.already_claimed||r.today_checked_in)return r.stale?'warn':'ok';return 'warn'}
  function checkinText(a){const r=checkinOf(a);if(!r)return checkinLoading.value?'读取中':'未知';if(r.claimed)return '刚领 '+credit(r.credit);if(r.already_claimed||r.today_checked_in)return '今日已领';if(!r.ok)return '失败';return '可领取'}
  function claimText(r){if(r.claimed)return '已领取 '+credit(r.credit);if(r.already_claimed)return '今日已领';return r.message||'失败'}
  function shortTime(v){if(!v)return '-';return String(v).replace(/:00$/,'').slice(5,16)}
  function expireText(x){if(x.expired)return '已过期';if(x.days_to_expire===null||x.days_to_expire===undefined)return x.expire_time||'长期';if(x.days_to_expire<0)return '已过期';if(x.days_to_expire<=7)return x.days_to_expire+' 天内';return shortTime(x.expire_time)}
  function pkgBadge(x){if(x.expired)return 'err';if(Number(x.days_to_expire)>=0&&Number(x.days_to_expire)<=7)return 'inactive';return 'ok'}
  const rows=computed(()=>[...l.value].sort((a,b)=>(a.status==='active'?0:1)-(b.status==='active'?0:1)||(officialBalance(b)??-1)-(officialBalance(a)??-1)));
  const alerts=computed(()=>{const c=creditSum.value;if(!c)return[];return c.expiring_accounts?.length?c.expiring_accounts:(c.low_accounts||[])});
  onMounted(load);return{l,ld,cld,checkins,checkinSummary,checkinLoading,claimingAll,officialRefreshing,pkg,claim,busyKey,withBusy,load,loadCredit,loadCheckins,refreshAllResources,refreshResource,openPackages,refreshPackages,claimOne,claimAll,rows,alerts,creditSum,fmt,credit,age,officialBalance,officialMeta,cacheAge,officialWarn,expireMeta,checkinOf,checkinClass,checkinText,claimText,shortTime,expireText,pkgBadge,chName,I}
},template:`
<div>
  <div class="phead"><h1>额度与积分</h1><p>官方余额 · 额度包明细 · 每日积分领取（每日领取的 150 按官方返回的约 1 个月到期时间展示）</p></div>
  <div class="tbar"><button class="btn s" @click="refreshAllResources()" :disabled="officialRefreshing||!l.some(a=>a.status==='active')">{{officialRefreshing?'刷新中':'刷新官方额度'}}</button><button class="btn s" @click="loadCheckins(true)" :disabled="checkinLoading">{{checkinLoading?'读取中':'刷新领取状态'}}</button><button class="btn s" @click="loadCredit(true)" :disabled="cld">{{cld?'刷新中':'刷新汇总缓存'}}</button><button class="btn s pri" @click="claimAll" :disabled="claimingAll||!l.some(a=>a.status==='active')">{{claimingAll?'领取中':'一键领取今日积分'}}</button><div class="spacer"></div><span class="tag" v-if="checkinSummary">可领 {{checkinSummary.available}} · 已领 {{checkinSummary.already_claimed}}</span></div>
  <div v-if="ld" class="load"><div class="spin"></div></div>
  <template v-else>
    <div class="dash-grid">
      <div class="card">
        <div class="card-h">官方额度概览<span class="sub">{{creditSum?.ok_accounts||0}}/{{creditSum?.active_accounts||0}} 账号已读取 · 缓存 {{creditSum?.stale_accounts||0}}</span></div>
        <div class="card-p">
          <div class="metric-row">
            <div class="metric" v-for="ch in (creditSum?.channels||[])" :key="ch.id"><div class="m-label">{{ch.display_name||ch.id}}</div><div class="m-value">{{ch.unit==='credit'&&ch.remaining!=null?credit(ch.remaining):'-'}}</div><div class="m-sub">积分 · {{ch.accounts||0}} 账号{{ch.unsupported?' · 无积分接口':''}}</div></div>
            <div class="metric" v-if="!(creditSum?.channels||[]).length"><div class="m-label">官方额度</div><div class="m-value">-</div><div class="m-sub">按通道分别统计，不跨厂加总</div></div>
            <div class="metric"><div class="m-label">7 天内到期</div><div class="m-value" :class="{'warn-text':Number(credit?.expiring_7d_total)>0}">{{credit(creditSum?.expiring_7d_total)}}</div><div class="m-sub">仅 WorkBuddy</div></div>
            <div class="metric"><div class="m-label">30 天内到期</div><div class="m-value" :class="{'warn-text':Number(credit?.expiring_30d_total)>0}">{{credit(creditSum?.expiring_30d_total)}}</div><div class="m-sub">{{creditSum?.package_count||0}} 个额度包</div></div>
          </div>
          <div class="status-line" style="margin-top:12px">
            <span class="badge" :class="credit?.failed_accounts?'warn':'ok'">失败 {{creditSum?.failed_accounts||0}}</span>
            <span class="badge" :class="credit?.stale_accounts?'warn':'ok'">旧缓存 {{creditSum?.stale_accounts||0}}</span>
            <span class="tag" v-if="creditSum?.updated_at">更新时间 {{fmt(creditSum.updated_at)}}</span>
          </div>
        </div>
      </div>
      <div class="card">
        <div class="card-h">额度提醒<span class="sub">低余额 / 即将到期</span></div>
        <div class="card-p rank-list" v-if="alerts.length">
          <div class="rank-item" v-for="a in alerts" :key="a.account_id">
            <div><div class="rank-name">{{a.account_name||a.account_id}}</div><div class="rank-meta">{{expireMeta(a)}}<span v-if="a.stale"> · 旧缓存 {{age(a.age_seconds)}}</span></div><div class="progress" :class="{err:a.expiring_30d_total>0}"><span :style="{width:Math.min(100,(a.expiring_30d_total||0)/Math.max(a.balance||1,1)*100)+'%'}"></span></div></div>
            <div class="rank-value">{{credit(a.balance)}}</div>
          </div>
        </div>
        <div class="empty" v-else>暂无低余额或即将到期提醒</div>
      </div>
    </div>
    <div class="card" v-if="l.length"><div class="card-h">账号额度与积分<span class="sub">官方余额来自各通道资源接口；领取为手动触发，不会定时领取，也不会绕过验证或风控</span></div><div class="table-scroll"><table><thead><tr><th>账号</th><th>通道</th><th>状态</th><th>今日领取</th><th>官方余额</th><th>额度包到期</th><th></th></tr></thead><tbody>
      <tr v-for="a in rows" :key="a.id"><td style="font-weight:600">{{a.nickname||a.name}}</td><td><span class="tag">{{chName(a)}}</span></td><td><span class="badge" :class="a.status">{{a.status}}</span></td><td><span class="badge" :class="checkinClass(a)">{{checkinText(a)}}</span><div class="cache-note" v-if="checkins[a.id]?.stale">旧缓存</div></td><td class="official-cell"><div class="official-main"><span class="official-val" v-if="officialBalance(a)!==null">{{credit(officialBalance(a))}}</span><span class="official-val unset" v-else>{{busyKey(a.id,'resource')?'读取中':(a.official_resource?.unsupported?'无积分':(a.official_resource&&!a.official_resource.ok?'失败':'未刷新'))}}</span></div><div class="official-meta" :class="{'pkg-warn':officialWarn(a)}">{{officialMeta(a)}}</div><div class="cache-note" v-if="a.official_resource">缓存 {{cacheAge(a)}}</div></td><td class="official-cell"><div class="official-main"><span class="official-val" :class="{'pkg-warn':officialWarn(a)}">{{credit(a.official_resource?.expiring_30d_total)}}</span><span class="tag" v-if="a.official_resource?.next_expire_time">{{shortTime(a.official_resource.next_expire_time)}}</span></div><div class="official-meta">最近到期 {{credit(a.official_resource?.next_expire_amount)}} · {{a.official_resource?.next_expire_days??'-'}} 天</div></td><td><div class="ops"><button class="btn s" @click="openPackages(a)" :disabled="busyKey(a.id,'resource')">{{busyKey(a.id,'resource')?'读取中':'额度明细'}}</button><button class="btn s" @click="claimOne(a)" :disabled="busyKey(a.id,'claim')||a.status!=='active'">{{busyKey(a.id,'claim')?'领取中':'领取'}}</button></div></td></tr>
      <tr v-if="!rows.length"><td colspan="7" class="empty">暂无账号</td></tr>
    </tbody></table></div></div>
    <div class="card card-p empty" v-else><div class="em">🔌</div><p>暂无账号 · 先在「账号管理」页导入</p></div>
  </template>
  <div class="ov" v-if="pkg" @click.self="pkg=null"><div class="modal wide"><div class="modal-h"><h3>官方额度明细 · {{pkg.account.nickname||pkg.account.name}}</h3><button class="x" @click="pkg=null">&times;</button></div><div class="modal-b"><div class="pkg-grid"><div class="pkg-kpi"><div class="k">官方余额</div><div class="v">{{credit(pkg.resource.total_dosage)}}</div></div><div class="pkg-kpi"><div class="k">30 天内到期</div><div class="v" :class="{'pkg-warn':Number(pkg.resource.expiring_30d_total||0)>0}">{{credit(pkg.resource.expiring_30d_total)}}</div></div><div class="pkg-kpi"><div class="k">额度包</div><div class="v">{{pkg.resource.package_count||0}}</div></div></div><div class="testbox" v-if="!pkg.resource.ok"><div class="row"><span>状态</span><span><span class="badge err">失败</span></span></div><div class="msg">{{pkg.resource.message||'官方额度读取失败'}}</div></div><table class="mini-table" v-else-if="pkg.resource.packages?.length"><thead><tr><th>额度包</th><th>剩余</th><th>已用</th><th>大小</th><th>周期</th><th>到期</th><th>状态</th></tr></thead><tbody><tr v-for="(x,i) in pkg.resource.packages" :key="i"><td><div class="pkg-name" :title="x.package_name">{{x.package_name}}</div><div class="hint">{{x.product_name||x.package_type||x.resource_type}}</div></td><td class="mono">{{credit(x.remaining_precise)}}</td><td class="mono">{{credit(x.cycle_used||x.used)}}</td><td class="mono">{{credit(x.cycle_size||x.size)}}</td><td class="pkg-time">{{shortTime(x.cycle_start)}} → {{shortTime(x.cycle_end)}}</td><td class="pkg-time" :class="{'pkg-warn':Number(x.days_to_expire)>=0&&Number(x.days_to_expire)<=30}">{{x.expire_time||'-'}}<div class="hint">{{expireText(x)}}</div></td><td><span class="badge" :class="pkgBadge(x)">{{x.expired?'已过期':'可用'}}</span></td></tr></tbody></table><div class="empty" v-else>暂无额度包明细</div><div class="hint" style="margin-top:10px">每日领取的 150 会作为独立额度资源进入这里；实际到期时间以官方返回的 CycleEndTime / DeductionEndTime 为准。</div></div><div class="modal-f"><button class="btn" @click="refreshPackages" :disabled="busyKey(pkg.account.id,'resource')">{{busyKey(pkg.account.id,'resource')?'刷新中':'刷新'}}</button><button class="btn pri" @click="pkg=null">关闭</button></div></div></div>
  <div class="ov" v-if="claim" @click.self="claim=null"><div class="modal"><div class="modal-h"><h3>{{claim.title}}</h3><button class="x" @click="claim=null">&times;</button></div><div class="modal-b"><div class="testbox" v-if="claim.summary"><div class="row"><span>账号</span><span>{{claim.summary.total}}</span></div><div class="row"><span>本次领取</span><span>{{claim.summary.claimed}} · {{credit(claim.summary.credit)}} credit</span></div><div class="row"><span>今日已领</span><span>{{claim.summary.already_claimed}}</span></div><div class="row"><span>失败</span><span>{{claim.summary.failed}}</span></div></div><table class="mini-table" style="margin-top:10px" v-if="claim.results?.length"><thead><tr><th>账号</th><th>结果</th><th>连续</th><th>HTTP</th></tr></thead><tbody><tr v-for="r in claim.results" :key="r.account_id"><td>{{r.account_name}}</td><td><span class="badge" :class="r.ok?'ok':'err'">{{claimText(r)}}</span></td><td>{{r.streak_days??'-'}}</td><td class="mono">{{r.status_code||'-'}}</td></tr></tbody></table><div class="hint" style="margin-top:8px">这是手动触发的领取动作；不会定时领取，也不会绕过验证或风控。</div></div><div class="modal-f"><button class="btn pri" @click="claim=null">关闭</button></div></div></div>
</div>`};
