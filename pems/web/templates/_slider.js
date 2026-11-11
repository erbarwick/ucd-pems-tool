  function attachCurtainSlider(box, overlayEl, handleEl) {
    if (!box || !overlayEl || !handleEl) return;
    var dragging = false;

    function update(clientX) {
      var rect = box.getBoundingClientRect();
      var pct = ((clientX - rect.left) / rect.width) * 100;
      pct = Math.max(0, Math.min(100, pct));
      overlayEl.style.clipPath = 'inset(0 ' + (100 - pct).toFixed(2) + '% 0 0)';
      handleEl.style.left = pct.toFixed(2) + '%';
    }

    box.addEventListener('mousedown', function(e) {
      dragging = true;
      update(e.clientX);
      e.preventDefault();
    });
    window.addEventListener('mouseup', function() { dragging = false; });
    window.addEventListener('mousemove', function(e) { if (dragging) update(e.clientX); });
    box.addEventListener('touchstart', function(e) {
      dragging = true;
      if (e.touches && e.touches[0]) update(e.touches[0].clientX);
    }, { passive: true });
    window.addEventListener('touchend', function() { dragging = false; });
    window.addEventListener('touchmove', function(e) {
      if (dragging && e.touches && e.touches[0]) update(e.touches[0].clientX);
    }, { passive: true });
    box.addEventListener('dblclick', function() {
      overlayEl.style.clipPath = 'inset(0 50% 0 0)';
      handleEl.style.left = '50%';
    });
  }
