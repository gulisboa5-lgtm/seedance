#!/usr/bin/env python3
"""Seedance 2.5 (Atlas Cloud) rodando local. Sem instalar nada: só Python 3.

Modos de uso:
  python3 seedance.py --web                       abre a telinha em 127.0.0.1:8777
  python3 seedance.py texto "um cachorro na praia"
  python3 seedance.py imagem foto.jpg "a cena ganha vida"
  python3 seedance.py referencia "ele dança" ref1.jpg ref2.jpg

Documentacao usada (api.atlascloud.ai, 16/09/2026):
  POST /api/v1/model/generateVideo      -> devolve data.id (a "ficha" do pedido)
  GET  /api/v1/model/prediction/<id>    -> data.status + data.outputs[0]
  POST /api/v1/model/uploadMedia        -> data.download_url (upload de arquivo local)
"""

import json
import base64
import http.cookies
import mimetypes
import os
import secrets
import threading
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PASTA = os.path.dirname(os.path.abspath(__file__))
SAIDAS = os.path.join(PASTA, "saidas")
BASE = "https://api.atlascloud.ai/api/v1/model"
# O Cloudflare da Atlas recusa o User-Agent padrao do Python (403, erro 1010).
NAVEGADOR = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")

MODELOS = {
    "texto": "bytedance/seedance-2.5/text-to-video",
    "imagem": "bytedance/seedance-2.5/image-to-video",
    "referencia": "bytedance/seedance-2.5/reference-to-video",
}

# ---------------------------------------------------------------- chave


def ler_chave():
    """Pega a chave do .env ao lado deste arquivo; variavel de ambiente ganha."""
    chave = os.environ.get("ATLASCLOUD_API_KEY", "").strip()
    if chave:
        return chave
    caminho = os.path.join(PASTA, ".env")
    if os.path.exists(caminho):
        for linha in open(caminho, encoding="utf-8"):
            linha = linha.strip()
            if linha.startswith("ATLASCLOUD_API_KEY="):
                return linha.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("Nao achei a chave. Coloque ATLASCLOUD_API_KEY no arquivo .env")


CHAVE = None  # preenchido no main


def cabecalho(json_body=True):
    h = {"Authorization": "Bearer " + CHAVE, "User-Agent": NAVEGADOR, "Accept": "application/json"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


# ---------------------------------------------------------------- rede


def _abrir(req, timeout=120):
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        corpo = e.read().decode("utf-8", "replace")
        raise RuntimeError("HTTP %s do servidor: %s" % (e.code, corpo[:500]))


def post_json(url, dados, timeout=120):
    req = urllib.request.Request(
        url, data=json.dumps(dados).encode("utf-8"), headers=cabecalho(), method="POST"
    )
    return _abrir(req, timeout)


def get_json(url, timeout=60):
    req = urllib.request.Request(url, headers=cabecalho(False), method="GET")
    return _abrir(req, timeout)


def enviar_arquivo(caminho):
    """Manda um arquivo do disco pro servidor e devolve a URL publica dele."""
    nome = os.path.basename(caminho)
    return enviar_bytes(nome, mimetypes.guess_type(nome)[0] or "", open(caminho, "rb").read())


def enviar_bytes(nome, tipo, conteudo):
    """Mesma coisa, mas com o arquivo ja em memoria (veio do navegador)."""
    tipo = tipo or mimetypes.guess_type(nome)[0] or "application/octet-stream"
    limite = "----seedance" + uuid.uuid4().hex
    corpo = b"".join([
        ("--%s\r\n" % limite).encode(),
        ('Content-Disposition: form-data; name="file"; filename="%s"\r\n' % nome).encode(),
        ("Content-Type: %s\r\n\r\n" % tipo).encode(),
        conteudo,
        ("\r\n--%s--\r\n" % limite).encode(),
    ])
    req = urllib.request.Request(
        BASE + "/uploadMedia",
        data=corpo,
        headers={
            "Authorization": "Bearer " + CHAVE,
            "Content-Type": "multipart/form-data; boundary=" + limite,
            "User-Agent": NAVEGADOR,
            "Accept": "application/json",
        },
        method="POST",
    )
    r = _abrir(req, timeout=300)
    url = (r.get("data") or {}).get("download_url")
    if not url:
        raise RuntimeError("upload nao devolveu download_url: " + json.dumps(r)[:300])
    return url


def entrada_de_midia(valor):
    """Aceita caminho de arquivo local (sobe) ou URL/asset:// (usa direto)."""
    valor = (valor or "").strip()
    if not valor:
        return None
    if valor.startswith(("http://", "https://", "asset://", "data:")):
        return valor
    if os.path.exists(valor):
        return enviar_arquivo(valor)
    raise RuntimeError("nao achei o arquivo: " + valor)


# ---------------------------------------------------------------- geracao


def gerar(modo, prompt, imagem=None, ultima_imagem=None, referencias=None,
          videos=None, audios=None, duracao=5, resolucao="720p", ratio="adaptive",
          com_audio=True, marca_dagua=False, formato="mp4", avisar=print):
    if modo not in MODELOS:
        raise RuntimeError("modo invalido: " + modo)

    dados = {
        "model": MODELOS[modo],
        "prompt": prompt,
        "duration": int(duracao),
        "resolution": resolucao,
        "ratio": ratio,
        "generate_audio": bool(com_audio),
        "watermark": bool(marca_dagua),
        "return_last_frame": False,
        "output_format": formato,
    }

    if modo == "imagem":
        dados["image"] = entrada_de_midia(imagem)
        if not dados["image"]:
            raise RuntimeError("o modo imagem precisa de uma imagem de primeiro quadro")
        fim = entrada_de_midia(ultima_imagem)
        if fim:
            dados["last_image"] = fim
    elif modo == "referencia":
        dados["reference_images"] = [entrada_de_midia(x) for x in (referencias or []) if x]
        dados["reference_videos"] = [entrada_de_midia(x) for x in (videos or []) if x]
        dados["reference_audios"] = [entrada_de_midia(x) for x in (audios or []) if x]
        dados["omni_reference_task_type"] = "auto"
        if not (dados["reference_images"] or dados["reference_videos"]):
            raise RuntimeError("o modo referencia precisa de ao menos 1 imagem ou video")

    avisar("Mandando o pedido...")
    r = post_json(BASE + "/generateVideo", dados)
    ficha = (r.get("data") or {}).get("id")
    if not ficha:
        raise RuntimeError("o servidor nao devolveu id: " + json.dumps(r)[:400])
    avisar("Pedido aceito. Ficha: %s" % ficha)
    return ficha


def esperar(ficha, avisar=print, teto_segundos=1800):
    """Fica perguntando ao servidor ate ficar pronto. Teto pra nao girar pra sempre."""
    comeco = time.time()
    ultimo = None
    while True:
        if time.time() - comeco > teto_segundos:
            raise RuntimeError("passou de %d min esperando a ficha %s" % (teto_segundos // 60, ficha))
        r = get_json(BASE + "/prediction/" + ficha)
        d = r.get("data") or {}
        status = d.get("status")
        if status != ultimo:
            avisar("Status: %s" % status)
            ultimo = status
        if status in ("completed", "succeeded"):
            saidas = d.get("outputs") or []
            if not saidas:
                raise RuntimeError("terminou sem arquivo de saida")
            return saidas[0]
        if status in ("failed", "canceled"):
            raise RuntimeError(d.get("error") or ("geracao %s" % status))
        time.sleep(3)


def baixar(url, nome=None):
    nome = nome or ("video-%s.mp4" % time.strftime("%Y%m%d-%H%M%S"))
    destino = os.path.join(SAIDAS, nome)
    os.makedirs(SAIDAS, exist_ok=True)
    pedido = urllib.request.Request(url, headers={"User-Agent": NAVEGADOR})
    with urllib.request.urlopen(pedido, timeout=600) as r, open(destino, "wb") as f:
        while True:
            pedaco = r.read(1 << 16)
            if not pedaco:
                break
            f.write(pedaco)
    return destino


# ---------------------------------------------------------------- telinha

# Senha de entrada. Vazia = sem senha (uso local). Na internet e OBRIGATORIA:
# sem ela, qualquer um que achar o endereco gera video com o seu saldo.
SENHA = os.environ.get("SEEDANCE_SENHA", "").strip()
SESSOES = {}          # bilhete -> quando expira
TRABALHOS = {}        # ficha -> {"status", "url", "arquivo", "erro"}
SALVAR_LOCAL = os.environ.get("SEEDANCE_SALVAR_LOCAL", "1") != "0"


def novo_bilhete():
    b = secrets.token_urlsafe(24)
    SESSOES[b] = time.time() + 30 * 24 * 3600
    return b


def bilhete_vale(cabecalhos):
    if not SENHA:
        return True
    bruto = cabecalhos.get("Cookie") or ""
    try:
        b = http.cookies.SimpleCookie(bruto).get("seed")
    except Exception:
        return False
    if not b:
        return False
    quando = SESSOES.get(b.value)
    if not quando or quando < time.time():
        SESSOES.pop(b.value, None)
        return False
    return True


ESTILO = """
:root{color-scheme:dark}
body{margin:0;background:#0d0f14;color:#e7e9ee;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
.caixa{max-width:720px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;margin:0 0 4px}p.sub{color:#8b93a7;margin:0 0 24px}
label{display:block;margin:16px 0 6px;font-weight:600;font-size:13px}
input,select,textarea{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:10px;
border:1px solid #262b38;background:#151925;color:#e7e9ee;font:inherit}
textarea{min-height:90px;resize:vertical}
.linha{display:flex;gap:12px}.linha>div{flex:1}
button{margin-top:22px;width:100%;padding:14px;border:0;border-radius:12px;background:#4f7cff;
color:#fff;font-weight:700;font-size:16px;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
#log{margin-top:20px;padding:12px;border-radius:10px;background:#151925;white-space:pre-wrap;
min-height:24px;font-family:ui-monospace,Menlo,monospace;font-size:13px;color:#9fb0d0}
video{width:100%;margin-top:16px;border-radius:12px;display:none}
a.baixar{display:none;margin-top:12px;color:#7fa4ff}
.aviso{margin-top:28px;font-size:12px;color:#6f7789;border-top:1px solid #1e2330;padding-top:14px}
.erro{color:#ff8b8b;margin-top:10px;min-height:20px}
"""

LOGIN = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Seedance 2.5</title>
<style>__ESTILO__</style></head><body><div class="caixa" style="max-width:360px;padding-top:80px">
<h1>Seedance 2.5</h1><p class="sub">Digite a senha para entrar.</p>
<input id="senha" type="password" placeholder="senha" autofocus>
<button id="ir">Entrar</button><div class="erro" id="erro"></div>
</div><script>
const entrar = async () => {
  const r = await fetch('/entrar', {method:'POST', headers:{'Content-Type':'application/json'},
                                    body: JSON.stringify({senha: document.getElementById('senha').value})});
  if (r.ok) location.reload();
  else document.getElementById('erro').textContent = 'Senha errada.';
};
document.getElementById('ir').onclick = entrar;
document.getElementById('senha').onkeydown = e => { if (e.key === 'Enter') entrar(); };
</script></body></html>""".replace("__ESTILO__", ESTILO)

PAGINA = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Seedance 2.5</title>
<style>__ESTILO__</style></head><body><div class="caixa">
<h1>Seedance 2.5</h1><p class="sub">Gera video com IA. Cada take gasta saldo da conta Atlas Cloud.</p>

<label>O que gerar</label>
<select id="modo">
  <option value="texto">Texto para video</option>
  <option value="imagem">Imagem para video (primeiro quadro)</option>
  <option value="referencia">Referencia para video (fotos do rosto)</option>
</select>

<label>Descricao da cena</label>
<textarea id="prompt" placeholder="Ex.: o homem das imagens num terraco em Dubai ao por do sol, luz de cinema, plano medio"></textarea>

<div id="campo-arquivos" style="display:none">
  <label id="rotulo-arquivos">Fotos</label>
  <input id="arquivos" type="file" accept="image/*,video/*,audio/*" multiple>
</div>

<div class="linha">
  <div><label>Duracao (segundos)</label><input id="duracao" type="number" min="4" max="30" value="5"></div>
  <div><label>Qualidade</label><select id="resolucao">
    <option>480p</option><option selected>720p</option><option>1080p</option></select></div>
</div>
<div class="linha">
  <div><label>Formato</label><select id="ratio">
    <option value="adaptive" selected>automatico</option><option value="16:9">16:9 deitado</option>
    <option value="9:16">9:16 em pe</option><option value="1:1">1:1 quadrado</option></select></div>
  <div><label>Som</label><select id="audio">
    <option value="1" selected>com audio</option><option value="0">sem audio</option></select></div>
</div>

<button id="ir">Gerar video</button>
<div id="log"></div>
<video id="player" controls></video>
<a class="baixar" id="baixar" download>Baixar o video</a>

<p class="aviso">Use so com imagem sua ou de quem autorizou por escrito. Video de gente real
sem permissao, ou feito pra passar por verdadeiro, e problema legal de quem publica.</p>
</div>
<script>
const $ = s => document.querySelector(s);
$('#modo').onchange = () => {
  const m = $('#modo').value;
  $('#campo-arquivos').style.display = m === 'texto' ? 'none' : 'block';
  $('#rotulo-arquivos').textContent = m === 'imagem'
    ? 'Imagem do primeiro quadro' : 'Fotos do rosto (2 a 4: frontal e perfis)';
  $('#arquivos').multiple = m !== 'imagem';
};

const lerArquivo = f => new Promise((ok, nao) => {
  const r = new FileReader();
  r.onload = () => ok({nome: f.name, tipo: f.type, dados: r.result.split(',')[1]});
  r.onerror = nao;
  r.readAsDataURL(f);
});

const subir = async arquivos => {
  const urls = [];
  for (const f of arquivos) {
    $('#log').textContent = 'Enviando ' + f.name + '...';
    const r = await fetch('/subir', {method:'POST', headers:{'Content-Type':'application/json'},
                                     body: JSON.stringify(await lerArquivo(f))});
    const j = await r.json();
    if (!j.ok) throw new Error(j.erro);
    urls.push(j.url);
  }
  return urls;
};

$('#ir').onclick = async () => {
  $('#ir').disabled = true; $('#player').style.display = 'none'; $('#baixar').style.display = 'none';
  try {
    const modo = $('#modo').value;
    let urls = [];
    if (modo !== 'texto') {
      const fs = $('#arquivos').files;
      if (!fs.length) throw new Error('escolha pelo menos um arquivo');
      urls = await subir(fs);
    }
    const corpo = {
      modo, prompt: $('#prompt').value, duracao: +$('#duracao').value,
      resolucao: $('#resolucao').value, ratio: $('#ratio').value,
      com_audio: $('#audio').value === '1'
    };
    if (modo === 'imagem') corpo.imagem = urls[0];
    if (modo === 'referencia') corpo.referencias = urls;

    $('#log').textContent = 'Mandando o pedido...';
    const r = await fetch('/gerar', {method:'POST', headers:{'Content-Type':'application/json'},
                                     body: JSON.stringify(corpo)});
    const j = await r.json();
    if (!j.ok) throw new Error(j.erro);

    // Pergunta de 4 em 4 segundos. Nao segura conexao aberta: na internet ela cairia.
    while (true) {
      await new Promise(p => setTimeout(p, 4000));
      const s = await (await fetch('/situacao?ficha=' + j.ficha)).json();
      if (s.status === 'pronto') {
        $('#log').textContent = 'Pronto.' + (s.arquivo ? ' Salvo em: ' + s.arquivo : '');
        $('#player').src = s.url; $('#player').style.display = 'block';
        $('#baixar').href = s.url; $('#baixar').style.display = 'inline-block';
        break;
      }
      if (s.status === 'erro') throw new Error(s.erro);
      $('#log').textContent = 'Gerando... (' + (s.status || 'aguardando') + ')';
    }
  } catch (e) { $('#log').textContent = 'Deu erro: ' + e.message; }
  $('#ir').disabled = false;
};
$('#modo').onchange();
</script></body></html>""".replace("__ESTILO__", ESTILO)


def _trabalhar(ficha):
    """Roda em linha separada: espera o video e, se for local, baixa."""
    try:
        url = esperar(ficha, avisar=lambda m: TRABALHOS[ficha].update(status=m))
        arquivo = baixar(url) if SALVAR_LOCAL else None
        TRABALHOS[ficha] = {"status": "pronto", "url": url, "arquivo": arquivo}
    except Exception as e:
        TRABALHOS[ficha] = {"status": "erro", "erro": str(e)}


class Servidor(BaseHTTPRequestHandler):
    def _responder(self, codigo, tipo, corpo, extra=None):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corpo)

    def _json(self, obj, codigo=200, extra=None):
        self._responder(codigo, "application/json", json.dumps(obj).encode("utf-8"), extra)

    def _corpo(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")

    def do_GET(self):
        caminho = urllib.parse.urlparse(self.path)
        if caminho.path in ("/", "/index.html"):
            pagina = PAGINA if bilhete_vale(self.headers) else LOGIN
            return self._responder(200, "text/html; charset=utf-8", pagina.encode("utf-8"))
        if caminho.path == "/saude":          # o Render chama isto pra ver se esta de pe
            return self._json({"ok": True})
        if not bilhete_vale(self.headers):
            return self._json({"ok": False, "erro": "entre de novo"}, 401)
        if caminho.path == "/situacao":
            ficha = urllib.parse.parse_qs(caminho.query).get("ficha", [""])[0]
            return self._json(TRABALHOS.get(ficha) or {"status": "desconhecida"})
        return self._responder(404, "text/plain", b"nada aqui")

    def do_POST(self):
        if self.path == "/entrar":
            if SENHA and secrets.compare_digest(self._corpo().get("senha", ""), SENHA):
                return self._json({"ok": True}, 200, {
                    "Set-Cookie": "seed=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"
                                  % novo_bilhete()})
            time.sleep(1)     # atrasa quem fica tentando senha no chute
            return self._json({"ok": False}, 401)

        if not bilhete_vale(self.headers):
            return self._json({"ok": False, "erro": "entre de novo"}, 401)

        try:
            if self.path == "/subir":
                p = self._corpo()
                bruto = base64.b64decode(p.get("dados") or "")
                url = enviar_bytes(p.get("nome") or "arquivo", p.get("tipo") or "", bruto)
                return self._json({"ok": True, "url": url})

            if self.path == "/gerar":
                p = self._corpo()
                ficha = gerar(
                    p.get("modo", "texto"), p.get("prompt", ""),
                    imagem=p.get("imagem"), ultima_imagem=p.get("ultima_imagem"),
                    referencias=p.get("referencias"), duracao=p.get("duracao", 5),
                    resolucao=p.get("resolucao", "720p"), ratio=p.get("ratio", "adaptive"),
                    com_audio=p.get("com_audio", True), avisar=lambda m: None,
                )
                TRABALHOS[ficha] = {"status": "enviado"}
                threading.Thread(target=_trabalhar, args=(ficha,), daemon=True).start()
                return self._json({"ok": True, "ficha": ficha})
        except Exception as e:
            return self._json({"ok": False, "erro": str(e)})

        return self._responder(404, "text/plain", b"nada aqui")

    def log_message(self, *a):
        pass


def web(porta=None):
    # O Render manda a porta pela variavel PORT e exige escutar em 0.0.0.0.
    do_render = "PORT" in os.environ
    porta = porta or int(os.environ.get("PORT", 8777))
    endereco_escuta = "0.0.0.0" if do_render else "127.0.0.1"
    if do_render and not SENHA:
        raise SystemExit("Na internet e obrigatorio definir SEEDANCE_SENHA.")
    servidor = ThreadingHTTPServer((endereco_escuta, porta), Servidor)
    print("Telinha no ar em %s:%d   (Ctrl+C para parar)" % (endereco_escuta, porta), flush=True)
    # So abre o navegador quando alguem rodou na mao. Como servico (launchd) nao
    # tem terminal, e abrir aba a cada reinicio seria um inferno.
    if sys.stdout.isatty():
        try:
            webbrowser.open("http://127.0.0.1:%d" % porta)
        except Exception:
            pass
    servidor.serve_forever()


# ---------------------------------------------------------------- linha de comando


def main():
    global CHAVE
    CHAVE = ler_chave()
    args = sys.argv[1:]
    if not args or args[0] in ("--web", "-w"):
        return web(int(args[1]) if len(args) > 1 else 8777)

    modo = args[0]
    if modo not in MODELOS:
        raise SystemExit(__doc__)

    if modo == "texto":
        if len(args) < 2:
            raise SystemExit('uso: python3 seedance.py texto "sua descricao"')
        ficha = gerar("texto", args[1])
    elif modo == "imagem":
        if len(args) < 3:
            raise SystemExit('uso: python3 seedance.py imagem foto.jpg "sua descricao"')
        ficha = gerar("imagem", args[2], imagem=args[1])
    else:
        if len(args) < 3:
            raise SystemExit('uso: python3 seedance.py referencia "descricao" ref1.jpg ref2.mp4')
        ficha = gerar("referencia", args[1], referencias=args[2:])

    url = esperar(ficha)
    print("Salvo em: " + baixar(url))


if __name__ == "__main__":
    main()
