import {api,apiErr,n,tok,pct,money,ms,fmt} from '../api.js';
import {I} from '../icons.js';
const{ref,reactive,computed,onMounted}=Vue;

export default {props:['token','toast'],setup(p){
  const s=ref(null),credit=ref(null),overview=ref(null),ld=ref(true),cld=ref(false),err=ref(''),updatedAt=ref(''),todayChartMetric=ref('requests');
  async function load(forceCredit=false){ld.value=true;err.value='';try{const [stats,cred,ov]=await Promise.all([api.get('/admin/stats',p.token),api.get('/admin/credit-summary'+(forceCredit?'?force=1':''),p.token),api.get('/admin/credit-overview',p.token).catch(()=>null)]);s.value=stats;credit.value=cred;overview.value=ov;updatedAt.value=new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'})}catch(e){s.value=null;err.value=apiErr(e,'加载失败：'+e.message);p.toast(err.value,'err')}ld.value=false}
  async function refreshCredit(){if(cld.value)return;cld.value=true;try{credit.value=await api.get('/admin/credit-summary?force=1',p.token);updatedAt.value=new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});p.toast('官方额度已刷新')}catch(e){p.toast(apiErr(e,'官方额度刷新失败'),'err')}cld.value=false}
  function healthClass(){if(!s.value?.active_accounts||!s.value?.active_keys)return 'err';if((s.value?.today?.errors||0)>0||s.value?.filtered_requests>0)return 'warn';return ''}
  function healthText(){if(!s.value?.active_accounts)return '无可用账号';if(!s.value?.active_keys)return '无可用 API Key';if((s.value?.today?.errors||0)>0)return '有请求异常';if(s.value?.filtered_requests>0)return '存在内容过滤';return '运行正常'}
  function rateWidth(v){return Math.max(0,Math.min(100,Number(v||0)))+'%'}
  function age(v){v=Number(v||0);if(v<60)return v+'s';if(v<3600)return Math.floor(v/60)+'m';return Math.floor(v/3600)+'h'}
  function expireMeta(a){if(a.next_expire_days===null||a.next_expire_days===undefined)return '无明确到期';return a.next_expire_days+' 天 · '+(a.next_expire_time||'-')}
  const mx=computed(()=>s.value?.daily?.length?Math.max(...s.value.daily.map(d=>d.requests),1):1);
  const heatRows=computed(()=>{const d=s.value?.daily||[];return[
    {name:'调用',kind:'requests',values:d.map(x=>Number(x.requests||0)),max:Math.max(...d.map(x=>Number(x.requests||0)),1)},
    {name:'Token',kind:'tokens',values:d.map(x=>Number(x.tokens||0)),max:Math.max(...d.map(x=>Number(x.tokens||0)),1)},
    {name:'Credit',kind:'credit',values:d.map(x=>Number(x.credits||0)),max:Math.max(...d.map(x=>Number(x.credits||0)),1)},
    {name:'Work真值',kind:'tw',values:d.map(x=>Number(x.traework_credit||0)),max:Math.max(...d.map(x=>Number(x.traework_credit||0)),1)},
  ]});
  const todayLabel=computed(()=>new Date().toLocaleDateString('zh-CN',{month:'long',day:'numeric',weekday:'short'}));
  const todayUsage=computed(()=>{
    const t=s.value?.today||{},requests=Number(t.requests||0),daily=s.value?.daily||[];
    function points(kind){const key=kind==='credit'?'credits':kind;const values=daily.map(d=>({date:d.date,value:Number(d[key]||0)}));return{values,max:Math.max(...values.map(x=>x.value),1)}}
    const creditPoints=points('credit'),requestPoints=points('requests'),tokenPoints=points('tokens');
    return[
      {kind:'credit',label:'今日 Credit(官方标价估算)',value:money(t.credit),unit:'Credit',meta:n(requests)+' 次调用 · 单次 '+money(requests?Number(t.credit||0)/requests:0)+'（按官方三档标价公式；订阅内实际扣费远低于此，见文档§10）',foot:credit.value?.channels?.length?credit.value.channels.map(c=>c.id+' '+(c.unit==='credit'&&c.remaining!=null?money(c.remaining):'—')).join(' · '):'累计标价估算 '+money(s.value?.total_credit),footLabel:credit.value?.channels?.length?'按通道积分余额':'累计标价估算',icon:I.wallet,...creditPoints},
      {kind:'requests',label:'今日调用次数',value:n(requests),unit:'次',meta:'成功 '+n(t.success)+' · 异常 '+n(Number(t.errors||0)+Number(t.filtered||0)),foot:pct(t.success_rate),footLabel:'成功率',icon:I.activity,...requestPoints},
      {kind:'tokens',label:'今日 Token',value:tok(t.tokens),unit:'Tokens',meta:'单次平均 '+tok(requests?Math.round(Number(t.tokens||0)/requests):0),foot:'累计 '+tok(s.value?.total_tokens),footLabel:'全部时间',icon:I.tokens,...tokenPoints},
    ]
  });
  const todayChart=computed(()=>{
    const configs={requests:{label:'调用次数',unit:'次'},tokens:{label:'Token',unit:'tokens'},credit:{label:'Credit(标价估算)',unit:'Credit'}};
    const kind=todayChartMetric.value,config=configs[kind],currentHour=new Date().getHours();
    const rows=(s.value?.today?.hourly||[]).map(row=>({...row,value:Number(row[kind]||0)}));
    const elapsed=rows.filter(row=>Number(row.hour)<=currentHour),max=Math.max(...elapsed.map(row=>row.value),0);
    return{...config,kind,currentHour,max,maxLabel:heatValue(kind,max),activeHours:elapsed.filter(row=>row.value>0).length,rows}
  });
  function heatClass(v,max){if(!v)return'heat-0';const r=Math.max(0,Math.min(1,Number(v||0)/Math.max(Number(max||1),1));if(r<.25)return'heat-1';if(r<.5)return'heat-2';if(r<.75)return'heat-3';if(r<.9)return'heat-4';return'heat-5'}
  function heatValue(kind,v){return kind==='tokens'?tok(v):(kind==='credit'||kind==='tw'?money(v):n(v))}
  function cacheStatusLabel(s){return ({accurate:'含 cache 实测',partial:'部分实测+反推',approx:'cache 反推',empty:'无数据'})[s]||'未知'}
  function cacheStatusClass(s){return ({accurate:'cache-ok',partial:'cache-partial',approx:'cache-approx',empty:'cache-empty'})[s]||'cache-empty'}
  function sparkHeight(card,v){v=Number(v||0);return(v?Math.max(7,Math.round(v/Math.max(card.max,1)*36)):4)+'px'}
  function hourBarHeight(v,max){v=Number(v||0);return(v?Math.max(6,Math.round(v/Math.max(max,1)*156)):3)+'px'}
  onMounted(load);
  return{s,credit,overview,ld,cld,err,updatedAt,todayLabel,todayUsage,todayChartMetric,todayChart,load,refreshCredit,n,tok,pct,money,ms,fmt,healthClass,healthText,rateWidth,age,expireMeta,mx,heatRows,heatClass,heatValue,cacheStatusLabel,cacheStatusClass,sparkHeight,hourBarHeight,I}
},template:`
<div>
  <div class="phead"><h1>运行总览</h1><p>网关状态、额度和调用强度</p></div>
  <div class="tbar"><button class="btn s" @click="load(false)" :disabled="ld"><span v-html="I.refresh"></span>{{ld?'刷新中':'刷新'}}</button><button class="btn s" @click="refreshCredit" :disabled="cld||ld">{{cld?'刷新中':'强制刷新官方额度'}}</button><div class="spacer"></div><span class="tag" v-if="updatedAt">更新 {{updatedAt}}</span><span class="tag" v-if="s">Base URL · 127.0.0.1:8787/v1</span></div>
  <div v-if="ld" class="load"><div class="spin"></div></div>
  <div class="card card-p empty" v-else-if="err"><div class="em">!</div><p style="font-weight:600;color:var(--fg)">{{err}}</p><p style="margin-top:6px">本机访问通常刷新页面即可重新获取管理凭证。</p></div>
  <template v-else-if="s">
    <div class="dash-hero">
      <div>
        <div class="health-main"><span class="health-dot" :class="healthClass()"></span><div><div class="health-title">{{healthText()}}</div><div class="health-sub">本机 OpenAI 兼容网关 · {{s.active_accounts}}/{{s.total_accounts}} 账号可用 · {{s.active_keys}}/{{s.total_keys}} Key 可用</div></div></div>
        <div class="status-line">
          <span class="badge" :class="s.active_accounts?'ok':'err'">Accounts {{s.active_accounts}}</span>
          <span class="badge" :class="s.active_keys?'ok':'err'">Keys {{s.active_keys}}</span>
          <span class="badge" :class="s.today?.errors?'err':'ok'">Errors {{s.today?.errors||0}}</span>
          <span class="tag">Filtered {{s.today?.filtered||0}}</span>
        </div>
      </div>
      <div class="health-kpis">
        <div class="health-kpi"><div class="k">今日请求</div><div class="v">{{n(s.today?.requests)}}</div></div>
        <div class="health-kpi"><div class="k">今日成功率</div><div class="v">{{pct(s.today?.success_rate)}}</div></div>
        <div class="health-kpi"><div class="k">平均耗时</div><div class="v">{{ms(s.today?.avg_duration_ms)}}</div></div>
      </div>
    </div>

    <section class="today-usage" aria-labelledby="today-usage-title">
      <div class="today-usage-head"><div><h2 class="today-usage-title" id="today-usage-title">今日用量</h2><div class="today-usage-sub">全账号请求、Token 与额度消耗汇总</div></div><div class="today-period">{{todayLabel}} · 00:00 至今</div></div>
      <div class="today-metrics">
        <article class="today-metric" :class="card.kind" v-for="card in todayUsage" :key="card.kind">
          <div class="today-metric-top"><span class="today-metric-icon" v-html="card.icon"></span><div class="today-metric-label">{{card.label}}</div></div>
          <div class="today-value-row"><div class="today-value">{{card.value}}</div><div class="today-unit">{{card.unit}}</div></div>
          <div class="today-meta">{{card.meta}}</div>
          <div class="today-trend"><div class="today-spark" aria-label="近 7 日趋势"><span v-for="point in card.values" :key="point.date" :style="{height:sparkHeight(card,point.value)}" :title="point.date+' · '+heatValue(card.kind,point.value)"></span></div><div class="today-foot"><strong>{{card.foot}}</strong>{{card.footLabel}}</div></div>
        </article>
      </div>
    </section>

    <section class="card today-hour-chart" :class="todayChart.kind" aria-labelledby="today-chart-title">
      <div class="card-h today-chart-head"><div class="today-chart-title"><span id="today-chart-title">今日 24 小时趋势</span><span class="sub">按本机时间逐小时聚合</span></div><div class="today-chart-controls"><div class="seg"><button :class="{on:todayChartMetric==='requests'}" @click="todayChartMetric='requests'">调用</button><button :class="{on:todayChartMetric==='tokens'}" @click="todayChartMetric='tokens'">Token</button><button :class="{on:todayChartMetric==='credit'}" @click="todayChartMetric='credit'">额度</button></div></div></div>
      <div class="hour-chart-body">
        <div class="hour-chart-meta"><span>当前指标</span><strong>{{todayChart.label}}</strong><span>峰值 {{todayChart.maxLabel}} {{todayChart.unit}}</span><span>活跃 {{todayChart.activeHours}} 小时</span></div>
        <div class="hour-chart-canvas"><div class="hour-y-axis"><span>{{todayChart.maxLabel}}</span><span>{{heatValue(todayChart.kind,todayChart.max/2)}}</span><span>0</span></div><div class="hour-plot"><div class="hour-grid"><span></span><span></span><span></span></div><div class="hour-bars"><div class="hour-bar-slot" v-for="row in todayChart.rows" :key="row.hour" :title="row.label+' · '+heatValue(todayChart.kind,row.value)+' '+todayChart.unit"><span class="hour-bar" :class="{'has-value':row.value>0,current:row.hour===todayChart.currentHour,future:row.hour>todayChart.currentHour}" :style="{height:hourBarHeight(row.value,todayChart.max)}"></span></div></div><div class="hour-chart-empty" v-if="!todayChart.max"><span>今天暂无{{todayChart.label}}数据</span></div></div></div>
        <div class="hour-x-axis"><span>00:00</span><span>04:00</span><span>08:00</span><span>12:00</span><span>16:00</span><span>20:00</span><span>23:00</span></div>
      </div>
    </section>

    <div class="dash-grid wide" v-if="credit">
      <div class="card">
        <div class="card-h">官方额度概览<span class="sub">{{credit.ok_accounts}}/{{credit.active_accounts}} 账号已读取 · 缓存 {{credit.stale_accounts}}</span></div>
        <div class="card-p">
          <div class="metric-row">
            <div class="metric" v-for="ch in (credit.channels||[])" :key="ch.id"><div class="m-label">{{ch.display_name||ch.id}}</div><div class="m-value">{{ch.unit==='credit'&&ch.remaining!=null?money(ch.remaining):'—'}}</div><div class="m-sub">积分 · {{ch.accounts||0}} 账号{{ch.unsupported?' · 无积分接口':''}}</div></div>
            <div class="metric" v-if="!(credit.channels||[]).length"><div class="m-label">官方额度</div><div class="m-value">—</div><div class="m-sub">按通道分别统计，不跨厂加总</div></div>
            <div class="metric"><div class="m-label">7 天内到期</div><div class="m-value" :class="{'warn-text':Number(credit.expiring_7d_total)>0}">{{money(credit.expiring_7d_total)}}</div><div class="m-sub">仅 WorkBuddy</div></div>
            <div class="metric"><div class="m-label">30 天内到期</div><div class="m-value" :class="{'warn-text':Number(credit.expiring_30d_total)>0}">{{money(credit.expiring_30d_total)}}</div><div class="m-sub">{{credit.package_count}} 个额度包</div></div>
          </div>
          <div class="status-line" style="margin-top:12px">
            <span class="badge" :class="credit.failed_accounts?'warn':'ok'">失败 {{credit.failed_accounts}}</span>
            <span class="badge" :class="credit.stale_accounts?'warn':'ok'">旧缓存 {{credit.stale_accounts}}</span>
            <span class="tag">更新时间 {{fmt(credit.updated_at)}}</span>
          </div>
        </div>
      </div>
      <div class="card">
        <div class="card-h">额度提醒<span class="sub">低余额 / 即将到期</span></div>
        <div class="card-p rank-list" v-if="credit.low_accounts?.length||credit.expiring_accounts?.length">
          <div class="rank-item" v-for="a in (credit.expiring_accounts?.length?credit.expiring_accounts:credit.low_accounts)" :key="a.account_id">
            <div><div class="rank-name">{{a.account_name||a.account_id}}</div><div class="rank-meta">{{expireMeta(a)}}<span v-if="a.stale"> · 旧缓存 {{age(a.age_seconds)}}</span></div><div class="progress" :class="{err:a.expiring_30d_total>0}"><span :style="{width:rateWidth(Math.min(100,(a.expiring_30d_total||0)/Math.max(a.balance||1,1)*100))}"></span></div></div>
            <div class="rank-value">{{money(a.balance)}}</div>
          </div>
        </div>
        <div class="empty" v-else>暂无低余额或即将到期提醒</div>
      </div>
      <div class="card" v-if="overview&&overview.ok">
        <div class="card-h">账户历史总消耗（估算）<span class="sub">当前已用 + 已过期积分（假设过期部分已用完）</span></div>
        <div class="card-p">
          <div class="metric-row">
            <div class="metric"><div class="m-label">当前包已用</div><div class="m-value">{{money(overview.current_consumed)}}</div><div class="m-sub">账户级 · 官方</div></div>
            <div class="metric"><div class="m-label">已过期积分</div><div class="m-value">{{money(overview.expired_total)}}</div><div class="m-sub">{{overview.expired_count}} 个过期包 · 假设用完</div></div>
            <div class="metric"><div class="m-label">历史总消耗(估)</div><div class="m-value warn-text">{{money(overview.historical_estimate)}}</div><div class="m-sub">= 当前已用 + 过期</div></div>
          </div>
          <div class="hint" style="margin-top:10px">官方不回报"过期包实际用量"和"TraeSOLO 单产品消耗"，故历史总消耗为估算值，不可用于按产品/按日拆分；TraeWork 的精确消耗见上方真实明细。</div>
        </div>
      </div>
    </div>

    <div>
      <div class="card">
        <div class="card-h">7 天调用强度<span class="sub">Credit=官方标价估算（含 cache 实测/反推，见角标） · Work真值=TraeWork 官方实扣</span>
        <div class="status-line" style="margin-top:6px"><span class="tag" :class="cacheStatusClass(d.cache_status)" v-for="d in s.daily" :key="'cs-'+d.date">{{d.date.slice(5)}}: {{cacheStatusLabel(d.cache_status)}}</span></div>
        </div>
        <div class="chart-box" v-if="s.daily?.length">
          <div class="activity-scroll"><div class="activity-map">
            <div class="activity-corner">指标</div><div class="activity-head" v-for="d in s.daily" :key="'h-'+d.date">{{d.date.slice(5)}}</div>
            <template v-for="row in heatRows" :key="row.kind"><div class="activity-label">{{row.name}}</div><div class="activity-cell" :class="heatClass(v,row.max)" v-for="(v,i) in row.values" :key="row.kind+'-'+i"><strong>{{heatValue(row.kind,v)}}</strong></div></template>
          </div></div>
          <div class="heat-legend"><span>低</span><span class="heat-swatch heat-1"></span><span class="heat-swatch heat-2"></span><span class="heat-swatch heat-3"></span><span class="heat-swatch heat-4"></span><span class="heat-swatch heat-5"></span><span>高</span></div>
        </div>
        <div class="empty" v-else>暂无趋势数据</div>
      </div>
    </div>
  </template>
</div>`};
