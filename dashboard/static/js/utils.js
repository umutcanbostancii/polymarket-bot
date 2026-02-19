const $=s=>document.querySelector(s);
const $$=s=>document.querySelectorAll(s);

const ICONS={target:'\u{1F3AF}',shuffle:'\u{1F500}',zap:'\u26A1',clock:'\u23F0',
  sunrise:'\u{1F305}',star:'\u2B50',flame:'\u{1F525}',chart:'\u{1F4C8}'};

const FACTOR_LABELS={
  strong_delta:'Guclu Delta (>0.003)',medium_delta:'Orta Delta',weak_delta:'Zayif Delta (<0.0005)',
  early_entry:'Erken Giris (>120s)',mid_entry:'Orta Giris (60-120s)',late_entry:'Gec Giris (<60s)',
  strong_momentum:'Guclu Momentum',weak_momentum:'Zayif Momentum',
  momentum_aligned:'Momentum Uyumlu',momentum_divergent:'Momentum Ters'
};