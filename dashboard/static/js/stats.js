Dashboard.prototype.renderStats=function(s){
  if(!s)return;
  const pnl=s.net_pnl||0,profit=s.total_profit||0,loss=s.total_loss||0;
  const wr=s.win_rate||0,wins=s.wins||0,losses=s.losses||0;
  const total=s.total_trades||0,pending=s.pending||0,cost=s.total_cost||0;
  const balance=s.current_balance||(1000+pnl);
  const starting=s.starting_balance||1000;
  $('#s-wallet').textContent='$'+balance.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
  const walletEl=$('#s-wallet');
  walletEl.className='stat-value '+(balance>=starting?'gold':'negative');
  const pnlPct=starting>0?((balance-starting)/starting*100):0;
  $('#s-wallet-sub').textContent=`Baslangic: $${starting.toLocaleString()} (${pnlPct>=0?'+':''}${pnlPct.toFixed(1)}%)`;
  const pnlEl=$('#s-pnl');
  pnlEl.textContent=(pnl>=0?'+':'')+`$${pnl.toFixed(2)}`;
  pnlEl.className='stat-value '+(pnl>=0?'positive':'negative');
  const tp=s.today_pnl||0;
  $('#s-pnl-today').textContent=`Bugun: ${tp>=0?'+':''}$${tp.toFixed(2)}`;
  $('#s-profit').textContent='+$'+profit.toFixed(2);
  $('#s-profit-sub').textContent=`${wins} kazanan islem`;
  $('#s-loss').textContent='-$'+Math.abs(loss).toFixed(2);
  $('#s-loss-sub').textContent=`${losses} kaybeden islem`;
  const wrEl=$('#s-winrate');wrEl.textContent=wr.toFixed(1)+'%';
  wrEl.className='stat-value '+(wr>=50?'positive':wr>0?'negative':'');
  $('#s-winrate-sub').textContent=`${wins}W / ${losses}L`;
  $('#s-trades').textContent=total;
  $('#s-trades-sub').textContent=pending>0?`${pending} beklemede`:`Maliyet: $${cost.toFixed(0)}`;
};

Dashboard.prototype.renderLiveStats=function(s){
  if(!s)return;
  const balance=s.polymarket_balance;
  const walletEl=$('#ls-wallet');
  if(walletEl){
    if(balance>=0){
      walletEl.textContent='$'+balance.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
      walletEl.className='stat-value '+(balance>0?'gold':'negative');
      $('#ls-wallet-sub').textContent='Polymarket cuzdan';
    }else{
      walletEl.textContent='—';
      walletEl.className='stat-value';
      $('#ls-wallet-sub').textContent='Baglanti bekleniyor...';
    }
  }
  const pnl=s.net_pnl||0;
  const pnlEl=$('#ls-pnl');
  if(pnlEl){
    pnlEl.textContent=(pnl>=0?'+':'')+`$${pnl.toFixed(2)}`;
    pnlEl.className='stat-value '+(pnl>=0?'positive':'negative');
  }
  const tp=s.today_pnl||0;
  const todayEl=$('#ls-pnl-today');
  if(todayEl)todayEl.textContent=`Bugun: ${tp>=0?'+':''}$${tp.toFixed(2)}`;

  const profit=s.total_profit||0,loss=s.total_loss||0;
  const wins=s.wins||0,losses=s.losses||0;
  const profitEl=$('#ls-profit');
  if(profitEl)profitEl.textContent='+$'+profit.toFixed(2);
  const profitSub=$('#ls-profit-sub');
  if(profitSub)profitSub.textContent=`${wins} kazanan islem`;
  const lossEl=$('#ls-loss');
  if(lossEl)lossEl.textContent='-$'+Math.abs(loss).toFixed(2);
  const lossSub=$('#ls-loss-sub');
  if(lossSub)lossSub.textContent=`${losses} kaybeden islem`;

  const wr=s.win_rate||0;
  const wrEl=$('#ls-winrate');
  if(wrEl){wrEl.textContent=wr.toFixed(1)+'%';
    wrEl.className='stat-value '+(wr>=50?'positive':wr>0?'negative':'');}
  const wrSub=$('#ls-winrate-sub');
  if(wrSub)wrSub.textContent=`${wins}W / ${losses}L`;

  const total=s.total_trades||0,pending=s.pending||0;
  const trEl=$('#ls-trades');
  if(trEl)trEl.textContent=total;
  const trSub=$('#ls-trades-sub');
  if(trSub)trSub.textContent=pending>0?`${pending} beklemede`:'';

  const cost=s.total_cost||0;
  const volEl=$('#ls-volume');
  if(volEl)volEl.textContent='$'+cost.toFixed(2);
  const todayTrades=s.today_trades||0;
  const volSub=$('#ls-volume-sub');
  if(volSub)volSub.textContent=`Bugun: ${todayTrades} islem`;
};

Dashboard.prototype.renderStatus=function(s){
  if(!s)return;
  const badge=$('#status-badge'),text=$('#status-text');
  badge.className='status-badge '+(s.running?'running':'stopped');
  text.textContent=s.running?'Calisiyor':'Durdu';
  $('#mode-badge').textContent=s.mode||'PAPER';
  if(s.trade_size&&s.trade_size!==this.tradeSize)this.updateTradeSizeUI(s.trade_size);
  if(s.btc_price&&!this.btcPrice){this.btcPrice=s.btc_price;
    $('#s-btc').textContent='$'+s.btc_price.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}
};