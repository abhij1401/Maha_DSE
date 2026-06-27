/**
 * script.js
 * MAHA-DSE College Predictor · Main JavaScript
 * Author: Abhishek Jadhav
 */

'use strict';

/* ─── Utilities ─────────────────────────────────────────────── */

function qs(sel, ctx = document) { return ctx.querySelector(sel); }
function qsa(sel, ctx = document) { return [...ctx.querySelectorAll(sel)]; }

function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

/* ─── Navbar scroll behaviour ───────────────────────────────── */

(function initNavbar() {
  const navbar = qs('#navbar');
  const toggle = qs('#navToggle');
  const links  = qs('#navLinks');

  if (!navbar) return;

  window.addEventListener('scroll', () => {
    navbar.classList.toggle('scrolled', window.scrollY > 30);
  }, { passive: true });

  toggle?.addEventListener('click', () => {
    links?.classList.toggle('open');
    const spans = qsa('span', toggle);
    const isOpen = links?.classList.contains('open');
    if (spans.length === 3) {
      spans[0].style.transform = isOpen ? 'rotate(45deg) translate(5px,5px)' : '';
      spans[1].style.opacity   = isOpen ? '0' : '1';
      spans[2].style.transform = isOpen ? 'rotate(-45deg) translate(5px,-5px)' : '';
    }
  });

  // Close menu on nav link click (mobile)
  qsa('.nav-link').forEach(link => {
    link.addEventListener('click', () => links?.classList.remove('open'));
  });
})();


/* ─── Stat Counter Animation ────────────────────────────────── */

(function initCounters() {
  const counters = qsa('.stat-num[data-target]');
  if (!counters.length) return;

  const easeOut = (t) => 1 - Math.pow(1 - t, 3);

  const animateCounter = (el) => {
    const target = parseInt(el.dataset.target, 10);
    const suffix = el.dataset.suffix || '+';
    const duration = 1800;
    const start = performance.now();

    const tick = (now) => {
      const elapsed = now - start;
      const progress = Math.min(elapsed / duration, 1);
      const value = Math.round(easeOut(progress) * target);
      el.textContent = value + (progress < 1 ? '' : suffix);
      if (progress < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  };

  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        animateCounter(entry.target);
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.5 });

  counters.forEach(el => observer.observe(el));
})();


/* ─── Scroll reveal ─────────────────────────────────────────── */

(function initScrollReveal() {
  const cards = qsa('.feature-card, .about-card, .step-card, .contact-card');
  if (!cards.length) return;

  const style = document.createElement('style');
  style.textContent = `
    .sr-hidden { opacity: 0; transform: translateY(24px); transition: opacity 0.5s ease, transform 0.5s ease; }
    .sr-visible { opacity: 1; transform: translateY(0); }
  `;
  document.head.appendChild(style);

  cards.forEach((card, i) => {
    card.classList.add('sr-hidden');
    card.style.transitionDelay = `${(i % 3) * 80}ms`;
  });

  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('sr-visible');
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.1 });

  cards.forEach(card => observer.observe(card));
})();


/* ─── Branch dynamic loading ────────────────────────────────── */

(function initBranchLoader() {
  const yearSelect   = qs('#academic_year');
  const branchGrid   = qs('#branchGrid');
  const branchLoading = qs('#branchLoading');

  if (!yearSelect || !branchGrid) return;

  yearSelect.addEventListener('change', async () => {
    const year = yearSelect.value;
    if (!year) return;

    branchLoading?.classList.add('visible');
    branchGrid.style.opacity = '0.4';

    try {
      const res  = await fetch(`/api/branches/${year}`);
      const list = await res.json();
      renderBranches(list);
    } catch (err) {
      console.error('Failed to load branches:', err);
    } finally {
      branchLoading?.classList.remove('visible');
      branchGrid.style.opacity = '1';
    }
  });

  function renderBranches(branches) {
    branchGrid.innerHTML = branches.map(branch => `
      <label class="branch-chip">
        <input type="checkbox" name="preferred_branches" value="${escHtml(branch)}" />
        <span>${escHtml(branch)}</span>
      </label>
    `).join('');
  }

  function escHtml(str) {
    return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
})();


/* ─── Branch select/clear all ───────────────────────────────── */

(function initBranchControls() {
  const selectAll = qs('#selectAllBranches');
  const clearAll  = qs('#clearBranches');
  const getBoxes  = () => qsa('input[name="preferred_branches"]');

  selectAll?.addEventListener('click', () => {
    getBoxes().forEach(cb => cb.checked = true);
  });

  clearAll?.addEventListener('click', () => {
    getBoxes().forEach(cb => cb.checked = false);
  });
})();


/* ─── Form Validation & Submission ─────────────────────────── */

(function initForm() {
  const form       = qs('#predictForm');
  if (!form) return;

  const overlay    = qs('#loadingOverlay');
  const submitBtn  = qs('#submitBtn');
  const submitText = qs('#submitText');
  const submitSpinner = qs('#submitSpinner');

  const rules = {
    full_name:     { required: true, label: 'Full name' },
    mobile:        { required: true, pattern: /^\d{10}$/, label: 'Mobile number', msg: 'Enter a valid 10-digit mobile number' },
    gender:        { required: true, label: 'Gender' },
    domicile:      { required: true, label: 'Domicile' },
    percentile:    { required: true, min: 0, max: 100, label: 'Percentile' },
    caste:         { required: true, label: 'Caste category' },
    academic_year: { required: false, label: 'Academic year' },
    cap_round:     { required: false, label: 'CAP Round' },
  };

  function showError(fieldId, msg) {
    const el  = qs(`#err_${fieldId}`);
    const inp = qs(`#${fieldId}`, form);
    if (el)  el.textContent  = msg;
    if (inp) inp.classList.add('invalid');
  }

  function clearError(fieldId) {
    const el  = qs(`#err_${fieldId}`);
    const inp = qs(`#${fieldId}`, form);
    if (el)  el.textContent  = '';
    if (inp) inp.classList.remove('invalid');
  }

  function validateForm() {
    let valid = true;

    // Clear all errors
    Object.keys(rules).forEach(id => clearError(id));
    const branchErr = qs('#err_branches');
    if (branchErr) branchErr.textContent = '';

    // Validate each field
    Object.entries(rules).forEach(([id, rule]) => {
      const el = qs(`#${id}`, form);
      if (!el) return;

      const val = el.value.trim();

      if (rule.required && !val) {
        showError(id, `${rule.label} is required`);
        valid = false;
        return;
      }

      if (rule.pattern && val && !rule.pattern.test(val)) {
        showError(id, rule.msg || `Invalid ${rule.label}`);
        valid = false;
        return;
      }

      if (id === 'percentile' && val) {
        const num = parseFloat(val);
        if (isNaN(num) || num < 0 || num > 100) {
          showError(id, 'Percentile must be between 0 and 100');
          valid = false;
        }
      }
    });

    // Branch validation removed: empty selection = search all branches
    // (no blocking here)

    return valid;
  }

  // Live validation on blur
  Object.keys(rules).forEach(id => {
    const el = qs(`#${id}`, form);
    el?.addEventListener('blur', () => {
      // Re-validate just this field
      const rule = rules[id];
      clearError(id);
      const val = el.value.trim();
      if (rule.required && !val) {
        showError(id, `${rule.label} is required`);
      } else if (rule.pattern && val && !rule.pattern.test(val)) {
        showError(id, rule.msg || `Invalid ${rule.label}`);
      } else if (id === 'percentile' && val) {
        const num = parseFloat(val);
        if (isNaN(num) || num < 0 || num > 100) {
          showError(id, 'Percentile must be between 0 and 100');
        }
      }
    });
  });

  form.addEventListener('submit', (e) => {
    if (!validateForm()) {
      e.preventDefault();
      // Scroll to first error
      const firstErr = qs('.form-error:not(:empty)', form);
      if (firstErr) {
        firstErr.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
      return;
    }

    // Save name + mobile to sessionStorage for history
    const full_name = (qs('#full_name', form)?.value || '').trim();
    const mobile    = (qs('#mobile', form)?.value || '').trim();
    sessionStorage.setItem('maha_pending', JSON.stringify({ full_name, mobile }));

    // Show loading
    if (overlay) overlay.classList.add('active');
    if (submitBtn) submitBtn.disabled = true;
    if (submitText) submitText.textContent = 'Predicting…';
    if (submitSpinner) submitSpinner.classList.remove('hidden');
  });

  // Reset button
  qs('#resetBtn')?.addEventListener('click', () => {
    Object.keys(rules).forEach(id => clearError(id));
    const branchErr = qs('#err_branches');
    if (branchErr) branchErr.textContent = '';
  });
})();


/* ─── FAQ Accordion ─────────────────────────────────────────── */

(function initFAQ() {
  qsa('.faq-q').forEach(btn => {
    btn.addEventListener('click', () => {
      const item = btn.closest('.faq-item');
      const isOpen = item.classList.contains('open');

      // Close all
      qsa('.faq-item.open').forEach(i => {
        i.classList.remove('open');
        i.querySelector('.faq-q')?.setAttribute('aria-expanded', 'false');
      });

      // Open this one if it was closed
      if (!isOpen) {
        item.classList.add('open');
        btn.setAttribute('aria-expanded', 'true');
      }
    });
  });
})();


/* ─── Search History (localStorage) ────────────────────────── */

(function initHistory() {
  const STORAGE_KEY = 'maha_history';

  const historyEmpty  = qs('#historyEmpty');
  const historyTable  = qs('#historyTableWrap');
  const historyBody   = qs('#historyBody');
  const historyCount  = qs('#historyCount');
  const searchInput   = qs('#historySearch');
  const clearAllBtn   = qs('#clearAllHistory');

  if (!historyBody) return; // Not on the index page

  function getHistory() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
    } catch { return []; }
  }

  function saveHistory(arr) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(arr));
  }

  function deleteEntry(id) {
    if (!confirm('Delete this search record?')) return;
    const history = getHistory().filter(h => h.id !== id);
    saveHistory(history);
    renderHistory(searchInput?.value || '');
  }

  function rerunEntry(entry) {
    // Pre-fill the form fields
    const form = qs('#predictForm');
    if (!form) {
      window.location.href = '/#predict';
      return;
    }

    const set = (id, val) => {
      const el = qs(`#${id}`, form);
      if (el) el.value = val;
    };

    set('full_name',     entry.full_name    || '');
    set('mobile',        entry.mobile       || '');
    set('gender',        entry.gender       || '');
    set('domicile',      entry.domicile     || '');
    set('percentile',    entry.percentile   || '');
    set('caste',         entry.caste        || '');
    set('academic_year', entry.academic_year || '');
    set('cap_round',     entry.cap_round    || '');

    // Tick branches
    if (Array.isArray(entry.preferred_branches)) {
      qsa('input[name="preferred_branches"]', form).forEach(cb => {
        cb.checked = entry.preferred_branches.includes(cb.value);
      });
    }

    // Scroll to form
    qs('#predict')?.scrollIntoView({ behavior: 'smooth' });
  }

  function truncateBranches(branches) {
    if (!Array.isArray(branches) || !branches.length) return '—';
    if (branches.length <= 2) return branches.join(', ');
    return `${branches.slice(0, 2).join(', ')} +${branches.length - 2}`;
  }

  function renderHistory(filter = '') {
    let history = getHistory();
    const lf = filter.toLowerCase().trim();

    if (lf) {
      history = history.filter(h =>
        (h.full_name || '').toLowerCase().includes(lf) ||
        (h.mobile    || '').includes(lf)
      );
    }

    const total = getHistory().length;
    if (historyCount) {
      historyCount.textContent = `${total} search${total !== 1 ? 'es' : ''}`;
    }

    if (!history.length) {
      historyEmpty?.classList.add('visible');
      historyTable?.classList.remove('visible');
      return;
    }

    historyEmpty?.classList.remove('visible');
    historyTable?.classList.add('visible');

    historyBody.innerHTML = history.map((h, idx) => `
      <tr>
        <td>${idx + 1}</td>
        <td><span class="history-name">${escHtml(h.full_name || '—')}</span></td>
        <td><span class="history-mobile">${escHtml(h.mobile || '—')}</span></td>
        <td><span class="history-pct">${h.percentile}%ile</span></td>
        <td>${escHtml(h.caste || '—')}</td>
        <td>${escHtml(h.gender || '—')}</td>
        <td>${h.academic_year || '—'}</td>
        <td>Round ${h.cap_round || '—'}</td>
        <td class="history-branches" title="${(h.preferred_branches || []).join(', ')}">${escHtml(truncateBranches(h.preferred_branches))}</td>
        <td><span class="history-count-val">${h.colleges_found ?? '—'}</span></td>
        <td><span class="history-dt">${escHtml(h.datetime || '—')}</span></td>
        <td>
          <div class="history-action-btns">
            <button class="history-btn rerun" data-id="${h.id}" title="Re-run this prediction">
              <i class="fas fa-redo"></i> Rerun
            </button>
            <button class="history-btn delete" data-id="${h.id}" title="Delete this record">
              <i class="fas fa-trash"></i>
            </button>
          </div>
        </td>
      </tr>
    `).join('');

    // Attach action handlers
    qsa('.history-btn.rerun').forEach(btn => {
      btn.addEventListener('click', () => {
        const id   = parseInt(btn.dataset.id, 10);
        const entry = getHistory().find(h => h.id === id);
        if (entry) rerunEntry(entry);
      });
    });

    qsa('.history-btn.delete').forEach(btn => {
      btn.addEventListener('click', () => {
        deleteEntry(parseInt(btn.dataset.id, 10));
      });
    });
  }

  function escHtml(str) {
    if (str == null) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // Initial render
  renderHistory();

  // Live search filter
  searchInput?.addEventListener('input', debounce(() => {
    renderHistory(searchInput.value);
  }, 200));

  // Clear all
  clearAllBtn?.addEventListener('click', () => {
    const count = getHistory().length;
    if (!count) return;
    if (!confirm(`Clear all ${count} search record${count !== 1 ? 's' : ''}? This cannot be undone.`)) return;
    localStorage.removeItem(STORAGE_KEY);
    renderHistory();
  });
})();


/* ─── Active nav link highlight on scroll ───────────────────── */

(function initNavHighlight() {
  const sections = qsa('section[id]');
  const navLinks = qsa('.nav-link');
  if (!sections.length || !navLinks.length) return;

  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        const id = entry.target.id;
        navLinks.forEach(link => {
          const href = link.getAttribute('href');
          link.classList.toggle('active', href === `/#${id}` || href === `#${id}`);
        });
      }
    });
  }, { rootMargin: '-40% 0px -55% 0px' });

  sections.forEach(s => observer.observe(s));
})();


/* ─── Smooth scroll for anchor links ───────────────────────── */

document.addEventListener('click', (e) => {
  const link = e.target.closest('a[href^="#"], a[href*="/#"]');
  if (!link) return;

  const href = link.getAttribute('href');
  const hash = href.includes('#') ? '#' + href.split('#')[1] : href;
  const target = qs(hash);

  if (target) {
    e.preventDefault();
    target.scrollIntoView({ behavior: 'smooth' });
  }
});


/* ─── Hide loading overlay if navigating back ───────────────── */

window.addEventListener('pageshow', () => {
  const overlay = qs('#loadingOverlay');
  if (overlay) overlay.classList.remove('active');
  const submitBtn  = qs('#submitBtn');
  const submitText = qs('#submitText');
  const submitSpinner = qs('#submitSpinner');
  if (submitBtn)    submitBtn.disabled = false;
  if (submitText)   submitText.textContent = 'Predict My Colleges';
  if (submitSpinner) submitSpinner.classList.add('hidden');
});