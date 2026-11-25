(() => {
  const input = document.querySelector('#file-input'); const zone = document.querySelector('#drop-zone');
  const browse = document.querySelector('#browse-button'); const submit = document.querySelector('#analyze-button');
  const name = document.querySelector('#file-name'); const message = document.querySelector('#upload-message');
  const progress = document.querySelector('#progress-wrap'); const bar = document.querySelector('#progress-bar');
  const showFile = file => { if (!file) return; const ext = file.name.split('.').pop().toLowerCase(); if (!['log','txt'].includes(ext)) { message.textContent='Choose a .log or .txt file.'; message.className='form-message error'; input.value=''; submit.disabled=true; return; } name.textContent=`${file.name} · ${(file.size / 1024).toFixed(1)} KB`; submit.disabled=false; message.textContent=''; message.className='form-message'; };
  browse.addEventListener('click', () => input.click()); input.addEventListener('change', () => showFile(input.files[0]));
  ['dragenter','dragover'].forEach(e => zone.addEventListener(e, ev => { ev.preventDefault(); zone.classList.add('dragging'); }));
  ['dragleave','drop'].forEach(e => zone.addEventListener(e, ev => { ev.preventDefault(); zone.classList.remove('dragging'); }));
  zone.addEventListener('drop', e => { input.files=e.dataTransfer.files; showFile(input.files[0]); }); zone.addEventListener('keydown', e => { if(e.key==='Enter'||e.key===' ') input.click(); });
  document.querySelector('#upload-form').addEventListener('submit', async e => { e.preventDefault(); const file=input.files[0]; if(!file)return; submit.disabled=true; progress.hidden=false; bar.style.width='25%'; message.textContent='Uploading securely…'; message.className='form-message'; const data=new FormData(); data.append('file', file); try { const res=await fetch('/api/upload',{method:'POST',body:data}); bar.style.width='80%'; const body=await res.json(); if(!res.ok) throw new Error(body.error||'Upload failed.'); bar.style.width='100%'; message.textContent='Upload complete. Opening dashboard…'; message.className='form-message success'; window.location.assign(`/dashboard/${body.upload_id}`); } catch(err) { progress.hidden=true; submit.disabled=false; message.textContent=err.message; message.className='form-message error'; } });
})();
