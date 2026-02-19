Dashboard.prototype.onMarket=function(d){
  if(d.token_ids)this.marketTokenIds=d.token_ids;
  $('#no-market').style.display='none';$('#market-content').style.display='block';
  $('#m-name').textContent=d.question||'';
  $('#m-up').textContent=d.up_price?.toFixed(3)||'0.500';
  $('#m-down').textContent=d.down_price?.toFixed(3)||'0.500';
  $('#m-vol').textContent='$'+Math.round(d.volume||0).toLocaleString();
  $('#m-liq').textContent='$'+Math.round(d.liquidity||0).toLocaleString();
  this.marketEndTs=d.end_ts||0;this.marketStartTs=d.start_ts||0;
  this.updateCountdown();
  if(d.orderbook)this.renderDepth(d.orderbook);
  this.updateManualTradeSummary('5m');
};

Dashboard.prototype.onMarket15m=function(d){
  if(d.token_ids)this.market15mTokenIds=d.token_ids;
  $('#no-market-15m').style.display='none';
  $('#market-content-15m').style.display='block';
  $('#m-name-15m').textContent=d.question||'';
  $('#m-up-15m').textContent=d.up_price?.toFixed(3)||'0.500';
  $('#m-down-15m').textContent=d.down_price?.toFixed(3)||'0.500';
  $('#m-vol-15m').textContent='$'+Math.round(d.volume||0).toLocaleString();
  $('#m-liq-15m').textContent='$'+Math.round(d.liquidity||0).toLocaleString();
  this.market15mEndTs=d.end_ts||0;this.market15mStartTs=d.start_ts||0;
  this.updateCountdown();
  if(d.orderbook)this.renderDepth15m(d.orderbook);
  this.updateManualTradeSummary('15m');
};

Dashboard.prototype.onNoMarket=function(){
  $('#no-market').style.display='block';
  $('#no-market').textContent='Yeni market bekleniyor...';
  $('#market-content').style.display='none';
  this.marketEndTs=0;this.marketStartTs=0;
  this.marketTokenIds=[];
};

Dashboard.prototype.onNoMarket15m=function(){
  $('#no-market-15m').style.display='block';
  $('#no-market-15m').textContent='15dk market bekleniyor...';
  $('#market-content-15m').style.display='none';
  this.market15mEndTs=0;this.market15mStartTs=0;
  this.market15mTokenIds=[];
};

Dashboard.prototype.renderDepth=function(ob){
  const c=$('#depth-levels');
  const bids=(ob.bids||[]).slice(0,6),asks=(ob.asks||[]).slice(0,6);
  const maxRows=Math.max(bids.length,asks.length,1);
  let maxSize=0;
  [...bids,...asks].forEach(l=>{const s=parseFloat(l.size||l[1]||0);if(s>maxSize)maxSize=s});
  if(!maxSize)maxSize=1;
  let html='';
  for(let i=0;i<maxRows;i++){
    html+='<div class="depth-grid">';
    if(i<bids.length){const p=parseFloat(bids[i].price||bids[i][0]||0),
      s=parseFloat(bids[i].size||bids[i][1]||0),w=(s/maxSize*100).toFixed(0);
      html+=`<div class="depth-level bid"><span class="price">${p.toFixed(3)}</span>
        <span class="size">${s.toFixed(0)}</span><div class="bar" style="width:${w}%"></div></div>`;
    }else html+='<div class="depth-level bid"></div>';
    if(i<asks.length){const p=parseFloat(asks[i].price||asks[i][0]||0),
      s=parseFloat(asks[i].size||asks[i][1]||0),w=(s/maxSize*100).toFixed(0);
      html+=`<div class="depth-level ask"><span class="price">${p.toFixed(3)}</span>
        <span class="size">${s.toFixed(0)}</span><div class="bar" style="width:${w}%"></div></div>`;
    }else html+='<div class="depth-level ask"></div>';
    html+='</div>';
  }
  c.innerHTML=html;
};

Dashboard.prototype.renderDepth15m=function(ob){
  const c=$('#depth-levels-15m');
  if(!c)return;
  const bids=(ob.bids||[]).slice(0,5),asks=(ob.asks||[]).slice(0,5);
  const maxRows=Math.max(bids.length,asks.length,1);
  let maxSize=0;
  [...bids,...asks].forEach(l=>{const s=parseFloat(l.size||l[1]||0);if(s>maxSize)maxSize=s});
  if(!maxSize)maxSize=1;
  let html='';
  for(let i=0;i<maxRows;i++){
    html+='<div class="depth-grid">';
    if(i<bids.length){const p=parseFloat(bids[i].price||bids[i][0]||0),
      s=parseFloat(bids[i].size||bids[i][1]||0),w=(s/maxSize*100).toFixed(0);
      html+=`<div class="depth-level bid"><span class="price">${p.toFixed(3)}</span>
        <span class="size">${s.toFixed(0)}</span><div class="bar" style="width:${w}%"></div></div>`;
    }else html+='<div class="depth-level bid"></div>';
    if(i<asks.length){const p=parseFloat(asks[i].price||asks[i][0]||0),
      s=parseFloat(asks[i].size||asks[i][1]||0),w=(s/maxSize*100).toFixed(0);
      html+=`<div class="depth-level ask"><span class="price">${p.toFixed(3)}</span>
        <span class="size">${s.toFixed(0)}</span><div class="bar" style="width:${w}%"></div></div>`;
    }else html+='<div class="depth-level ask"></div>';
    html+='</div>';
  }
  c.innerHTML=html;
};

Dashboard.prototype.updateCountdown=function(){
  // 5m countdown
  if(this.marketEndTs){
    const now=Date.now()/1000,remaining=Math.max(0,this.marketEndTs-now);
    const total=this.marketEndTs-this.marketStartTs,elapsed=total-remaining;
    const pct=total>0?Math.min(100,(elapsed/total)*100):0;
    const min=Math.floor(remaining/60),sec=Math.floor(remaining%60);
    const el=$('#m-countdown');
    if(remaining<=0){
      el.textContent='0:00';el.className='countdown urgent';
      const bar=$('#m-progress');bar.style.width='100%';bar.className='fill late';
      $('#m-name').textContent=$('#m-name').textContent.split(' \u00B7')[0]+' \u00B7 Sonuclaniyor...';
    }else{
      el.textContent=`${min}:${sec.toString().padStart(2,'0')}`;
      el.className='countdown'+(remaining<30?' urgent':'');
      const bar=$('#m-progress');bar.style.width=pct+'%';
      bar.className='fill '+(pct<50?'early':pct<80?'mid':'late');
    }
  }
  // 15m countdown
  if(this.market15mEndTs){
    const now=Date.now()/1000,remaining=Math.max(0,this.market15mEndTs-now);
    const total=this.market15mEndTs-this.market15mStartTs,elapsed=total-remaining;
    const pct=total>0?Math.min(100,(elapsed/total)*100):0;
    const min=Math.floor(remaining/60),sec=Math.floor(remaining%60);
    const el=$('#m-countdown-15m');
    if(remaining<=0){
      el.textContent='0:00';el.className='countdown urgent';
      const bar=$('#m-progress-15m');bar.style.width='100%';bar.className='fill late';
    }else{
      el.textContent=`${min}:${sec.toString().padStart(2,'0')}`;
      el.className='countdown'+(remaining<60?' urgent':'');
      const bar=$('#m-progress-15m');bar.style.width=pct+'%';
      bar.className='fill '+(pct<50?'early':pct<80?'mid':'late');
    }
  }
};