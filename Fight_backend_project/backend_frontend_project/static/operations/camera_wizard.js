(() => {
  const form = document.getElementById('camera-wizard');
  if (!form) return;
  const steps = [...form.querySelectorAll('.wizard-step')];
  const progress = [...form.querySelectorAll('.wizard-progress li')];
  const back = form.querySelector('[data-wizard-back]');
  const next = form.querySelector('[data-wizard-next]');
  const save = form.querySelector('[data-wizard-save]');
  let current = 0;
  // Native validation is retained, but hidden steps must be revealed before focus.
  form.noValidate = true;
  function show(index, focus = true) {
    current = index;
    steps.forEach((step, i) => {step.hidden = i !== index;});
    progress.forEach((item, i) => {if (i === index) item.setAttribute('aria-current', 'step'); else item.removeAttribute('aria-current');});
    back.hidden = index === 0; next.hidden = index === steps.length - 1; save.hidden = !next.hidden;
    if (focus) {const legend = steps[index].querySelector('legend'); legend.tabIndex = -1; legend.focus();}
  }
  function validate(step) {
    const invalid = [...step.querySelectorAll('input,select,textarea')].find(input => !input.checkValidity());
    if (!invalid) return true;
    const speedPanel = invalid.closest('#speedModuleConfigWrapper');
    if (speedPanel) speedPanel.classList.add('is-visible');
    let parent = invalid.parentElement;
    while (parent && parent !== form) {if (parent.tagName === 'DETAILS') parent.open = true; parent = parent.parentElement;}
    invalid.reportValidity(); return false;
  }
  back.addEventListener('click', () => show(current - 1));
  next.addEventListener('click', () => {if (validate(steps[current])) show(current + 1);});
  form.addEventListener('submit', event => {
    if (current < steps.length - 1) {event.preventDefault(); if (validate(steps[current])) show(current + 1); return;}
    for (let i = 0; i < steps.length; i++) {
      if (steps[i].querySelector(':invalid')) {event.preventDefault(); show(i); validate(steps[i]); return;}
    }
  });
  const errorStep = steps.findIndex(step => step.querySelector('.errorlist'));
  form.querySelectorAll('details').forEach(detail => {if (detail.querySelector('.errorlist')) detail.open = true;});
  show(errorStep < 0 ? 0 : errorStep, false);
})();
