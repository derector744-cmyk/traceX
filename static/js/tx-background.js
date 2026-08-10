// traceX — фоновая анимация: плавающие узлы-точки, соединяющиеся линиями,
// в духе графа связей. Реагирует на скролл (лёгкий параллакс) и на движение мыши.
(function () {
  const canvas = document.getElementById('tx-bg-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');

  let width, height, dpr;
  let points = [];
  let mouseX = -9999, mouseY = -9999;
  let scrollShift = 0;
  const COLOR = '136, 117, 255'; // --tx-accent в rgb

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = width + 'px';
    canvas.style.height = height + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    initPoints();
  }

  function initPoints() {
    const density = window.innerWidth < 640 ? 18000 : 11000;
    const count = Math.max(18, Math.min(70, Math.round((width * height) / density)));
    points = [];
    for (let i = 0; i < count; i++) {
      points.push({
        x: Math.random() * width,
        y: Math.random() * height,
        vx: (Math.random() - 0.5) * 0.18,
        vy: (Math.random() - 0.5) * 0.18,
        r: Math.random() * 1.6 + 0.6,
      });
    }
  }

  function step() {
    ctx.clearRect(0, 0, width, height);

    const parallax = scrollShift * 0.02;

    // обновляем позиции
    for (const p of points) {
      p.x += p.vx;
      p.y += p.vy;
      if (p.x < 0 || p.x > width) p.vx *= -1;
      if (p.y < 0 || p.y > height) p.vy *= -1;
    }

    // связи между близкими точками (эффект графа)
    const maxDist = 140;
    for (let i = 0; i < points.length; i++) {
      for (let j = i + 1; j < points.length; j++) {
        const a = points[i], b = points[j];
        const dx = a.x - b.x, dy = a.y - b.y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < maxDist) {
          const opacity = (1 - dist / maxDist) * 0.16;
          ctx.strokeStyle = `rgba(${COLOR}, ${opacity})`;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y + parallax);
          ctx.lineTo(b.x, b.y + parallax);
          ctx.stroke();
        }
      }
      // связь с курсором — лёгкое "притяжение" визуально
      const dxm = points[i].x - mouseX, dym = points[i].y - mouseY;
      const distM = Math.sqrt(dxm * dxm + dym * dym);
      if (distM < 160) {
        const opacity = (1 - distM / 160) * 0.35;
        ctx.strokeStyle = `rgba(${COLOR}, ${opacity})`;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(points[i].x, points[i].y + parallax);
        ctx.lineTo(mouseX, mouseY);
        ctx.stroke();
      }
    }

    // сами точки со свечением
    for (const p of points) {
      const grad = ctx.createRadialGradient(p.x, p.y + parallax, 0, p.x, p.y + parallax, p.r * 5);
      grad.addColorStop(0, `rgba(${COLOR}, 0.9)`);
      grad.addColorStop(1, `rgba(${COLOR}, 0)`);
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(p.x, p.y + parallax, p.r * 5, 0, Math.PI * 2);
      ctx.fill();

      ctx.fillStyle = `rgba(220, 221, 222, 0.8)`;
      ctx.beginPath();
      ctx.arc(p.x, p.y + parallax, p.r, 0, Math.PI * 2);
      ctx.fill();
    }

    requestAnimationFrame(step);
  }

  window.addEventListener('resize', resize);
  window.addEventListener('mousemove', (e) => { mouseX = e.clientX; mouseY = e.clientY; });
  window.addEventListener('mouseleave', () => { mouseX = -9999; mouseY = -9999; });
  window.addEventListener('scroll', () => {
    scrollShift = window.scrollY || document.documentElement.scrollTop || 0;
  }, { passive: true });

  // уважение к пользователям, которые просят меньше анимации
  const prefersReduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  resize();
  if (!prefersReduced) {
    requestAnimationFrame(step);
  }
})();
