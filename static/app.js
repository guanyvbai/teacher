(function () {
    const $ = (sel, root = document) => root.querySelector(sel);
    const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
    const CSRF = (window.PAGE && window.PAGE.csrfToken) || '';
  
    function openModal(dateStr) {
      const modal = $('#dayModal');
      $('#modalDate').textContent = dateStr;
      $('#lessonDate').value = dateStr;
  
      const sid = window.PAGE?.studentId;
      const studentSelect = $('#lessonStudent');
      if (sid) {
        studentSelect.value = sid;
        studentSelect.disabled = true;
      } else {
        studentSelect.disabled = false;
      }
  
      loadLessons(dateStr, sid);
  
      modal.classList.remove('hidden');
      modal.setAttribute('aria-hidden', 'false');
    }
  
    function closeModal() {
      const modal = $('#dayModal');
      modal.classList.add('hidden');
      modal.setAttribute('aria-hidden', 'true');
      $('#lessonList').innerHTML = '<span class="muted small">加载中…</span>';
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
      $$('#dayModal [data-close]').forEach(el => el.addEventListener('click', closeModal));
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
        // 取参数
        const studentId = document.getElementById('lessonStudent')?.value;
        const dateStr   = document.getElementById('lessonDate')?.value;
        const timeStr   = form.querySelector('input[name="lesson_time"]')?.value;
        const duration  = form.querySelector('input[name="duration"]')?.value;
    
        // 参数不全就让后端报错
        if (!studentId || !dateStr || !timeStr || !duration) return;
    
        try {
          const url = new URL('/api/lessons/check_conflict', window.location.origin);
          url.searchParams.set('student_id', studentId);
          url.searchParams.set('date', dateStr);
          url.searchParams.set('time', timeStr);
          url.searchParams.set('duration', duration);
    
          const res = await fetch(url.toString(), { credentials: 'same-origin' });
          const data = await res.json();
    
          if (data && data.ok && data.conflicts && data.conflicts.length > 0) {
            const list = data.conflicts.map(c => `${c.start}-${c.end}`).join('；');
            const ok = window.confirm(`该时段与已排课程冲突：${list}\n\n确定仍然提交吗？`);
            if (!ok) {
              e.preventDefault();
              e.stopPropagation();
            }
          }
          // 若无冲突，则正常提交
        } catch (err) {
          // 预检失败不阻塞提交，交给后端硬校验兜底
          console.warn('conflict precheck failed:', err);
        }
      }, { capture: true });
    }
  })();
  