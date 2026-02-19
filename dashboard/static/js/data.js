Dashboard.prototype.loadInitialData=async function(){
  try{
    const[klines,trades,stats,status,analytics,liveStats]=await Promise.all([
      fetch(`/api/klines?interval=${this.currentInterval}&limit=300`).then(r=>r.json()),
      fetch('/api/trades?limit=1000&strategies=main').then(r=>r.json()),
      fetch('/api/stats').then(r=>r.json()),
      fetch('/api/status').then(r=>r.json()),
      fetch('/api/analytics').then(r=>r.json()),
      fetch('/api/live-stats').then(r=>r.json()).catch(()=>({})),
    ]);
    this.setKlines(klines);
    this.trades=trades;this.stats=stats;this.analytics=analytics;
    this.renderTrades();this.renderStats(stats);this.renderStatus(status);
    this.renderAnalytics(analytics);this.renderLiveStats(liveStats);
  }catch(e){console.error('Load error:',e)}
};

Dashboard.prototype.loadKlines=async function(interval){
  try{
    const k=await fetch(`/api/klines?interval=${interval}&limit=300`).then(r=>r.json());
    this.setKlines(k);
  }catch(e){}
};

Dashboard.prototype.refreshAnalytics=async function(){
  try{
    const[analytics,stats]=await Promise.all([
      fetch('/api/analytics').then(r=>r.json()),
      fetch('/api/stats').then(r=>r.json()),
    ]);
    this.analytics=analytics;this.stats=stats;
    this.renderAnalytics(analytics);this.renderStats(stats);
    if(this.activeView==='test')this.refreshTestAnalytics();
  }catch(e){}
};