(function () {
    const $ = (sel, root = document) => root.querySelector(sel);
    const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
    const CSRF = (window.PAGE && window.PAGE.csrfToken) || '';
  
    function closeModal() {
      const modal = $('#dayModal');
      if (!modal) return;
      modal.classList.add('hidden');
      modal.setAttribute('aria-hidden', 'true');
    }

    function openModal(dateStr) {
      const modal = $('#dayModal');
      $('#modalDate').textContent = dateStr;
      $('#lessonDate').value = dateStr;
    
      const sid = window.PAGE?.studentId;
      const studentSelect = $('#lessonStudent');
      const studentHidden = $('#lessonStudentHidden');
    
      if (sid) {
        studentSelect.value = sid;
        studentSelect.disabled = true;          // 锁定下拉
        if (studentHidden) studentHidden.value = sid; // ✅ 确保表单能提交 student_id
      } else {
        studentSelect.disabled = false;
        if (studentHidden) studentHidden.value = studentSelect.value || '';
      }
    
      // 如果用户在首页修改了下拉的学生，顺手同步隐藏域
      studentSelect.addEventListener('change', () => {
        if (studentHidden) studentHidden.value = studentSelect.value || '';
      }, { once: true });
    
      loadLessons(dateStr, sid);
    
      modal.classList.remove('hidden');
      modal.setAttribute('aria-hidden', 'false');
    }
  
    async function loadLessons(dateStr, sid) {
      const list = $('#lessonList');
      list.textContent = '加载中…';
      try {
        const url = new URL('/api/lessons', window.location.origin);
        url.searchParams.set('date', dateStr);
        if (sid) url.searchParams.set('sid', sid);
        const res = await fetch(url.toString(), { credentials: 'same-origin' });
        const data = await res.json();
        if (!data.ok) {
          list.innerHTML = `<span class="muted small">加载失败：${data.error || 'unknown'}</span>`;
          return;
        }
        if (!data.items.length) {
          list.innerHTML = `<span class="muted small">当天暂无排课</span>`;
          return;
        }
        list.innerHTML = '';
        data.items.forEach(item => {
          const div = document.createElement('div');
          div.className = 'timetable-item';
          if (item.status !== 'planned') div.classList.add('done');
          div.innerHTML = `
            <div class="ti-time">${item.time} · ${item.duration.toFixed(2)}h</div>
            <div class="ti-name">${item.student_name || ''}</div>
            ${item.note ? `<div class="ti-note">${item.note}</div>` : ''}
            <div class="ti-actions" style="margin-top:.25rem">
              ${item.status === 'planned' ? `
                <form method="post" action="/lessons/${item.id}/done" style="display:inline">
                  <input type="hidden" name="csrf_token" value="${CSRF}">
                  <button type="submit" class="btn small">完成</button>
                </form>` : ''}
              <form method="post" action="/lessons/${item.id}/delete" style="display:inline" onsubmit="return confirm('删除这条排课？');">
                <input type="hidden" name="csrf_token" value="${CSRF}">
                <button type="submit" class="danger small">删除</button>
              </form>
            </div>
          `;
          list.appendChild(div);
        });
      } catch (e) {
        list.innerHTML = `<span class="muted small">加载失败：${e && e.message ? e.message : e}</span>`;
      }
    }
  
    function bindCalendarClicks() {
      $$('.cal-cell[data-clickable]').forEach(cell => {
        cell.addEventListener('click', (ev) => {
          if (ev.target.closest('button, a, form')) return;
          const dateStr = cell.getAttribute('data-date');
          if (dateStr) openModal(dateStr);
        });
      });
    }
  
    function bindModalClose() {
      $$('#dayModal [data-close]').forEach(el => el.addEventListener('click', (ev) => {
        ev.preventDefault();
        closeModal();
      }));
      document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });
    }
  
    document.addEventListener('DOMContentLoaded', () => {
      bindCalendarClicks();
      bindModalClose();
      bindLessonFormGuard();
    });
    function bindLessonFormGuard() {
      const form = document.getElementById('lessonForm');
      if (!form) return;
      form.addEventListener('submit', async (e) => {
        const dateStr   = document.getElementById('lessonDate')?.value;
        const timeStr   = form.querySelector('input[name="lesson_time"]')?.value;
        const duration  = form.querySelector('input[name="duration"]')?.value;
    
        if (!dateStr || !timeStr || !duration) return;
    
        try {
          const url = new URL('/api/lessons/check_conflict', window.location.origin);
          url.searchParams.set('date', dateStr);
          url.searchParams.set('time', timeStr);
          url.searchParams.set('duration', duration);
    
          const res = await fetch(url.toString(), { credentials: 'same-origin' });
          const data = await res.json();
    
          if (data && data.ok && data.conflicts && data.conflicts.length > 0) {
            const list = data.conflicts.map(c => `${c.student ? (c.student + '：') : ''}${c.start}-${c.end}`).join('；');
            const ok = window.confirm(`该时段已被占用：${list}\n\n确定仍然提交吗？`);
            if (!ok) {
              e.preventDefault();
              e.stopPropagation();
            }
          }
        } catch (err) {
          const go = window.confirm('无法完成冲突预检，是否仍然提交？（后端会再次检验）');
          if (!go) {
            e.preventDefault();
            e.stopPropagation();
          }
        }
      }, { capture: true });
    }
  })();
  