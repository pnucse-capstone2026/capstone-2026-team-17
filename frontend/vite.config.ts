import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig, loadEnv } from 'vite';

export default defineConfig(({ mode }) => {
  // 개발 서버에서는 브라우저가 같은 origin의 /api를 호출하고 Vite가 FastAPI로 전달한다.
  // run-easydep이 실제 백엔드 포트를 주입하므로 개발자가 포트를 바꿔도 소스 수정이 없다.
  const apiOrigin = loadEnv(mode, '.', '').EASYDEP_API_ORIGIN ?? 'http://127.0.0.1:8100';
  return {
    plugins: [tailwindcss(), sveltekit()],
    server: {
      proxy: {
        '/api': apiOrigin
      }
    }
  };
});
