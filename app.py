# app.py
from flask import Flask, render_template, request, jsonify
import os
from datetime import datetime, date
import pytz
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import logging
import xml.etree.ElementTree as ET
import re



# -----------------------------------------------------------------------------
# LOGGING
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("app.log")]
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# CONSTANTES
# -----------------------------------------------------------------------------
API_URL = "https://dadosabertos.camara.leg.br/api/v2"
PLENARIO_ID = 180
URL_REQUERIMENTOS = "https://www.camara.leg.br/pplen/requerimentos-proposicao.html"
URL_DESTAQUES = "https://www.camara.leg.br/pplen/destaques.html"
URL_PARECERES = "https://www.camara.leg.br/proposicoesWeb/prop_pareceres_substitutivos_votos"

# -----------------------------------------------------------------------------
# HTTP SESSION (com Retry)
# -----------------------------------------------------------------------------
def build_session():
    s = requests.Session()
    retries = Retry(
        total=5,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({
        "Accept": "text/html,application/json",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    return s

SESSION = build_session()

# -----------------------------------------------------------------------------
# CACHE TTL SIMPLES
# -----------------------------------------------------------------------------
_CACHE = {}
_TTL_SECONDS = 300

def _now():
    from time import time
    return time()

def _cache_get(key):
    v = _CACHE.get(key)
    if not v:
        return None
    val, ts = v
    if _now() - ts > _TTL_SECONDS:
        _CACHE.pop(key, None)
        return None
    return val

def _cache_set(key, val):
    # evita crescimento infinito (máx ~200 itens)
    if len(_CACHE) > 200:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = (val, _now())

# -----------------------------------------------------------------------------
# UTILS
# -----------------------------------------------------------------------------
def _parse_datetime_flex(dt_str):
    if not dt_str:
        return "N/D"
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        pass
    try:
        dt = datetime.strptime(dt_str.split("+")[0], "%Y-%m-%dT%H:%M:%S")
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        try:
            dt = datetime.strptime(dt_str, "%d/%m/%Y")
            return dt.strftime("%d/%m/%Y")
        except Exception:
            return dt_str[:16].replace("T", " ") if len(dt_str) >= 16 else dt_str

def _get(d, *path, default=""):
    cur = d
    for p in path:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return default
    return cur if cur is not None else default

# --- helper para extrair a proposição principal do item da pauta (trata PPP e PEP) ---
def _principal_from_item(item_raw):
    prop = _get(item_raw, "proposicao_", default={}) or {}
    relacionada = _get(item_raw, "proposicaoRelacionada_", default={}) or {}

    sigla_tipo = _get(prop, "siglaTipo", default="")
    cod_tipo   = _get(prop, "codTipo", default=None)
    is_relacionada = (sigla_tipo in ["PPP", "PEP"]) or (cod_tipo in [192, 442])  # <-- Correção: inclui PEP (sigla/cod 442)

    if is_relacionada and relacionada:
        principal_id  = _get(relacionada, "id")
        ementa_ok     = _get(relacionada, "ementa", default="")
    else:
        principal_id  = _get(prop, "id")
        ementa_ok     = _get(prop, "ementa", default="")

    return principal_id, ementa_ok

# -----------------------------------------------------------------------------
# MODELOS (objetos simples)
# -----------------------------------------------------------------------------
class Evento:
    def __init__(self, id_evento, data_hora_inicio, situacao, descricao_tipo, descricao):
        self.id_evento = id_evento
        self.data_hora_inicio = _parse_datetime_flex(data_hora_inicio)
        self.situacao = situacao
        self.descricao_tipo = descricao_tipo
        self.descricao = descricao

class DestaqueEmenda:
    def __init__(self, numero, autoria, descricao, tipo_destaque, situacao):
        self.numero = numero
        self.autoria = autoria
        self.descricao = descricao
        self.tipo_destaque = tipo_destaque
        self.situacao = situacao

class ParecerSubstitutivoVoto:
    def __init__(self, tipo_proposicao, data_apresentacao, autor, descricao, link_inteiro_teor):
        self.tipo_proposicao = tipo_proposicao
        self.data_apresentacao = data_apresentacao
        self.autor = autor
        self.descricao = descricao or "Descrição não disponível"
        self.link_inteiro_teor = link_inteiro_teor

class Procedimento:
    def __init__(self, numero, autoria, descricao, situacao, data):
        self.numero = numero
        self.autoria = autoria
        self.descricao = descricao
        self.situacao = situacao
        self.data = data

# -----------------------------------------------------------------------------
# SERVIÇOS DE DADOS
# -----------------------------------------------------------------------------
def obter_eventos_dia(data_str):
    """Busca eventos do PLEN no dia e filtra Sessão Deliberativa."""
    ck = f"eventos:{data_str}"
    c = _cache_get(ck)
    if c is not None:
        return c
    try:
        url = f"{API_URL}/eventos"
        params = {
            "idOrgao": PLENARIO_ID,
            "dataInicio": data_str,
            "dataFim": data_str,
            "ordem": "ASC",
            "ordenarPor": "dataHoraInicio",
        }
        r = SESSION.get(url, params=params, timeout=15)
        r.raise_for_status()
        try:
            dados = r.json().get("dados", []) or []
        except ValueError:
            # fallback XML
            root = ET.fromstring(r.text)
            dados = []
            for e in root.findall(".//evento_"):
                dados.append({c.tag: c.text for c in e})

        eventos_delib = [
            Evento(
                id_evento=e.get("id", ""),
                data_hora_inicio=e.get("dataHoraInicio", ""),
                situacao=e.get("situacao", "Não Informada"),
                descricao_tipo=e.get("descricaoTipo", ""),
                descricao=e.get("descricao", "")
            )
            for e in dados
            if isinstance(e.get("descricaoTipo"), str) and "Sessão Deliberativa" in e.get("descricaoTipo")
        ]
        res = {
            "tem_sessao": len(eventos_delib) > 0,
            "eventos": eventos_delib,
            "erro": None if eventos_delib else f"Nenhuma sessão deliberativa em {data_str}."
        }
        _cache_set(ck, res)
        return res
    except Exception as e:
        res = {"tem_sessao": False, "eventos": [], "erro": f"Erro na API: {e}"}
        _cache_set(ck, res)
        return res

def obter_detalhes_proposicao(id_proposicao):
    ck = f"prop:{id_proposicao}"
    c = _cache_get(ck)
    if c is not None:
        return c
    try:
        r = SESSION.get(f"{API_URL}/proposicoes/{id_proposicao}", timeout=12)
        r.raise_for_status()
        try:
            d = r.json().get("dados", {}) or {}
        except ValueError:
            root = ET.fromstring(r.text)
            d = {c.tag: c.text for c in root.find(".//proposicao_") or []}
        descricao = _get(d, "statusProposicao", "descricaoSituacao", default="Não Informada")
        payload = {"descricao_situacao": descricao}
        _cache_set(ck, payload)
        return payload
    except Exception as e:
        return {"descricao_situacao": f"Erro: {e}"}

def obter_autores_proposicao(id_proposicao):
    ck = f"autores:{id_proposicao}"
    c = _cache_get(ck)
    if c is not None:
        return c
    try:
        r = SESSION.get(f"{API_URL}/proposicoes/{id_proposicao}/autores", timeout=12)
        r.raise_for_status()
        try:
            dados = r.json().get("dados", []) or []
        except ValueError:
            root = ET.fromstring(r.text)
            dados = []
            for el in root.findall(".//autor_"):
                dados.append({c.tag: c.text for c in el})
        autores = [{"nome": (a.get("nome") or (a.get("autor") or {}).get("nome") or "Desconhecido")} for a in dados[:2]]
        result = {"autores": autores, "tem_mais_autores": len(dados) > 2}
        _cache_set(ck, result)
        return result
    except Exception:
        result = {"autores": [], "tem_ais_autores": False}
        _cache_set(ck, result)
        return result

def obter_destaques_emendas(id_proposicao):
    ck = f"destaques:{id_proposicao}"
    c = _cache_get(ck)
    if c is not None:
        return c
    try:
        url = f"{URL_DESTAQUES}?codOrgao={PLENARIO_ID}&codProposicao={id_proposicao}"
        r = SESSION.get(url, timeout=20)
        if r.status_code != 200:
            raise Exception(f"HTTP {r.status_code}")
        soup = BeautifulSoup(r.text, "lxml")
        destaques = []
        for tabela in soup.find_all("table"):
            rows = tabela.find_all("tr")[1:]
            for row in rows:
                cols = row.find_all("td")
                if len(cols) < 5:
                    continue
                situacao = cols[4].text.strip()
                if situacao != "Em tramitação":
                    continue
                numero = cols[0].text.strip()
                autoria = cols[1].text.strip()
                descricao = (cols[2].text or "").strip() or "Descrição não disponível"
                tipo = cols[3].text.strip()
                destaques.append(DestaqueEmenda(numero, autoria, descricao, tipo, situacao))
        res = {
            "tem_destaques_emendas": len(destaques) > 0,
            "destaques_emendas": destaques,
            "erro": None if destaques else "Nenhum destaque/emenda em tramitação"
        }
        _cache_set(ck, res)
        return res
    except Exception as e:
        res = {"tem_destaques_emendas": False, "destaques_emendas": [], "erro": f"Erro no scraping: {e}"}
        _cache_set(ck, res)
        return res

def obter_pareceres_substitutivos_votos(id_proposicao):
    """
    Captura SOMENTE PRLP e PRLE. 
    - Se só houver PRLP -> retorna apenas o PRLP mais recente (maior número).
    - Se houver PRLP e PRLE -> retorna o PRLP mais recente e o PRLE mais recente.
    O link aponta para a página de histórico da proposição (estável).
    """
    ck = f"pareceres:{id_proposicao}"
    c = _cache_get(ck)
    if c is not None:
        return c

    try:
        url = f"{URL_PARECERES}?idProposicao={id_proposicao}"
        r = SESSION.get(url, timeout=25)
        if r.status_code != 200:
            raise Exception(f"HTTP {r.status_code}")

        soup = BeautifulSoup(r.text, "lxml")

        def normtxt(s):
            return (s or "").strip()

        candidatos = []  # [{"tipo": "PRLP"/"PRLE", "numero": int, ...}]
        for tabela in soup.find_all("table"):
            ths = [normtxt(th.get_text(" ", strip=True)).lower() for th in tabela.find_all("th")]
            if not ths:
                continue

            def idx_contains(substr):
                for i, h in enumerate(ths):
                    if substr in h:
                        return i
                return -1

            idx_psv  = idx_contains("pareceres")
            idx_tipo = idx_contains("tipo de propos")
            idx_data = idx_contains("data de apres")
            idx_aut  = idx_contains("autor")
            idx_desc = idx_contains("descri")

            if min(idx_psv, idx_tipo, idx_data, idx_aut, idx_desc) < 0 and len(ths) >= 5:
                idx_psv, idx_tipo, idx_data, idx_aut, idx_desc = 0, 1, 2, 3, 4

            if min(idx_psv, idx_tipo, idx_data, idx_aut, idx_desc) < 0:
                continue

            for tr in tabela.find_all("tr")[1:]:
                tds = tr.find_all("td")
                if len(tds) < max(idx_desc, idx_aut, idx_data, idx_tipo, idx_psv) + 1:
                    continue

                col_psv  = tds[idx_psv]
                col_data = tds[idx_data]
                col_aut  = tds[idx_aut]
                col_desc = tds[idx_desc]

                txt_psv = normtxt(col_psv.get_text(" ", strip=True))
                m = re.search(r"\b(PRL[PE])\s*(\d+)\b", txt_psv)
                if not m:
                    continue

                tipo_tag = m.group(1)   # "PRLP" ou "PRLE"
                if tipo_tag not in ("PRLP", "PRLE"):
                    continue

                numero   = int(m.group(2))
                data_ap  = normtxt(col_data.get_text(" ", strip=True)) or "N/D"
                autor    = normtxt(col_aut.get_text(" ", strip=True)) or "N/D"

                # descrição (limpa qualquer "Inteiro teor" que venha junto)
                descricao_raw = normtxt(col_desc.get_text(" ", strip=True)) or "Descrição não disponível"
                descricao = descricao_raw.replace("Inteiro teor", "").strip() or "Descrição não disponível"

                # Link estável para a página de histórico da proposição
                link_hist = f"https://www.camara.leg.br/proposicoesWeb/prop_pareceres_substitutivos_votos?idProposicao={id_proposicao}"

                candidatos.append({
                    "tipo": tipo_tag,
                    "numero": numero,
                    "tipo_proposicao": f"{tipo_tag} {numero}",
                    "data_apresentacao": data_ap,
                    "autor": autor,
                    "descricao": descricao,
                    "link_inteiro_teor": link_hist,
                })

        logger.info(f"pareceres {id_proposicao}: PRLP/PRLE encontrados = {len(candidatos)}")

        if not candidatos:
            res = {"tem_pareceres": False, "pareceres_substitutivos_votos": [], "erro": "Nenhum PRLP/PRLE encontrado"}
            _cache_set(ck, res)
            return res

        # Separa por tipo e pega o de maior número em cada tipo
        prlp = [c for c in candidatos if c["tipo"] == "PRLP"]
        prle = [c for c in candidatos if c["tipo"] == "PRLE"]

        selecionados = []
        if prlp:
            best_prlp = max(prlp, key=lambda x: x["numero"])
            selecionados.append(best_prlp)
        if prle:
            best_prle = max(prle, key=lambda x: x["numero"])
            selecionados.append(best_prle)

        # Converte para o modelo esperado (lista de objetos/ dicts)
        items = [
            ParecerSubstitutivoVoto(
                tipo_proposicao=sel["tipo_proposicao"],
                data_apresentacao=sel["data_apresentacao"],
                autor=sel["autor"],
                descricao=sel["descricao"],
                link_inteiro_teor=sel["link_inteiro_teor"],
            )
            for sel in selecionados
        ]

        res = {"tem_pareceres": len(items) > 0, "pareceres_substitutivos_votos": items, "erro": None}
        _cache_set(ck, res)
        return res

    except Exception as e:
        res = {"tem_pareceres": False, "pareceres_substitutivos_votos": [], "erro": f"Erro no scraping: {e}"}
        _cache_set(ck, res)
        return res



def obter_procedimentos_regimentais(id_proposicao):
    ck = f"proced:{id_proposicao}"
    c = _cache_get(ck)
    if c is not None:
        return c
    try:
        url = f"{URL_REQUERIMENTOS}?codOrgao={PLENARIO_ID}&codProposicao={id_proposicao}"
        r = SESSION.get(url, timeout=20)
        if r.status_code != 200:
            raise Exception(f"HTTP {r.status_code}")
        soup = BeautifulSoup(r.text, "lxml")
        procs = []
        for tabela in soup.find_all("table"):
            for row in tabela.find_all("tr")[1:]:
                cols = row.find_all("td")
                if len(cols) < 5:
                    continue
                situ = cols[3].text.strip()
                if situ != "Em tramitação":
                    continue
                data_raw = cols[4].text.strip()
                try:
                    data_fmt = datetime.strptime(data_raw, "%d/%m/%Y").strftime("%d/%m/%Y")
                except Exception:
                    data_fmt = data_raw or "N/D"
                procs.append(Procedimento(
                    numero=cols[0].text.strip(),
                    autoria=cols[1].text.strip(),
                    descricao=cols[2].text.strip(),
                    situacao=situ,
                    data=data_fmt
                ))
        res = {
            "tem_procedimentos": len(procs) > 0,
            "procedimentos": procs,
            "erro": None if procs else "Nenhum requerimento procedimental em tramitação"
        }
        _cache_set(ck, res)
        return res
    except Exception as e:
        res = {"tem_procedimentos": False, "procedimentos": [], "erro": f"Erro no scraping: {e}"}
        _cache_set(ck, res)
        return res

def obter_pauta_por_evento(evento_id):
    ck = f"pauta:{evento_id}"
    c = _cache_get(ck)
    if c is not None:
        return c
    try:
        r = SESSION.get(f"{API_URL}/eventos/{evento_id}/pauta", timeout=15)
        r.raise_for_status()
        try:
            dados_pauta = r.json().get("dados", []) or []
        except ValueError:
            root = ET.fromstring(r.text)
            dados_pauta = [{c.tag: c.text for c in item} for item in root.findall(".//item_") or []]

        logger.info(f"evento {evento_id}: itens na pauta = {len(dados_pauta)}")

        itens = []
        seen = set()
        for item in dados_pauta:
            principal_id, ementa_ok = _principal_from_item(item)
            if not principal_id or principal_id in seen:
                continue
            seen.add(principal_id)

            det   = obter_detalhes_proposicao(principal_id)
            autores = obter_autores_proposicao(principal_id)

            itens.append({
                "ordem": _get(item, "ordem", default="N/D"),
                "regime": _get(item, "regime", default=""),
                "titulo": _get(item, "titulo", default=""),
                "id_proposicao": principal_id,
                "ementa": ementa_ok,
                "relator_id": _get(item, "relator", "id", default=""),
                "relator_nome": _get(item, "relator", "nome", default=""),
                "relator_sigla_partido": _get(item, "relator", "siglaPartido", default=""),
                "relator_url_foto": _get(item, "relator", "urlFoto", default=""),
                "descricao_situacao": det.get("descricao_situacao"),
                "destaques_emendas": [],
                "procedimentos": [],
                "autores": autores,
                "pareceres_substitutivos_votos": [],
            })

        res = {"tem_pauta": len(itens) > 0, "itens": itens, "erro": None if itens else f"Sem itens para evento {evento_id}"}
        _cache_set(ck, res)
        return res
    except Exception as e:
        logger.exception(f"erro ao obter pauta do evento {evento_id}")
        res = {"tem_pauta": False, "itens": [], "erro": f"Erro na API: {e}"}
        _cache_set(ck, res)
        return res

# -----------------------------------------------------------------------------
# FLASK
# -----------------------------------------------------------------------------
app = Flask(__name__, static_url_path="/static", static_folder="static", template_folder="templates")

# Página shell (UI carrega via AJAX)
@app.route("/", methods=["GET"])
def home():
    hoje = date.today().strftime("%Y-%m-%d")
    return render_template("index.html", data=hoje)

# ---- APIs para o front AJAX ----

@app.route("/api/eventos/<data_str>", methods=["GET"])
def api_eventos(data_str):
    res = obter_eventos_dia(data_str)
    return jsonify({
        "tem_sessao": res["tem_sessao"],
        "erro": res["erro"],
        "eventos": [{
            "id_evento": e.id_evento,
            "data_hora_inicio": e.data_hora_inicio,
            "situacao": e.situacao,
            "descricao_tipo": e.descricao_tipo,
            "descricao": e.descricao
        } for e in res["eventos"]]
    })

@app.route("/api/pauta/<data_str>", methods=["GET"])
def api_pauta(data_str):
    """Retorna a pauta MESCLADA de todos os eventos deliberativos do dia.
       Opcional: ?evento_id=XXXX para pegar apenas um evento.
    """
    evento_id = request.args.get("evento_id")
    itens_merged = []
    eventos_res = obter_eventos_dia(data_str)
    if not eventos_res["tem_sessao"]:
        return jsonify({"tem_pauta": False, "itens": [], "erro": eventos_res["erro"], "eventos": []})

    eventos = [{"id_evento": e.id_evento, "situacao": e.situacao, "descricao_tipo": e.descricao_tipo,
                "data_hora_inicio": e.data_hora_inicio, "descricao": e.descricao} for e in eventos_res["eventos"]]

    alvo = [ev for ev in eventos if (not evento_id or ev["id_evento"] == evento_id)]
    for ev in alvo:
        pauta = obter_pauta_por_evento(ev["id_evento"])
        itens_merged.extend(pauta["itens"])

    return jsonify({
        "tem_pauta": len(itens_merged) > 0,
        "itens": itens_merged,
        "eventos": eventos,
        "erro": None if itens_merged else "Sem itens de pauta para o(s) evento(s) do dia."
    })

# Situação da proposição (para tooltips / atualizações pontuais)
@app.route("/api/proposicao/<int:proposicao_id>/situacao", methods=["GET"])
def api_proposicao_situacao(proposicao_id: int):
    try:
        r = SESSION.get(f"{API_URL}/proposicoes/{proposicao_id}", timeout=12)
        r.raise_for_status()
        j = r.json()
        dados = j.get("dados") or {}
        status = dados.get("statusProposicao") or {}
        descricao = status.get("descricaoSituacao") or dados.get("descricaoSituacao")
        return jsonify({"id": proposicao_id, "descricaoSituacao": descricao}), 200
    except requests.RequestException as e:
        return jsonify({"id": proposicao_id, "descricaoSituacao": None, "erro": f"Upstream: {e}"}), 502
    except Exception as e:
        return jsonify({"id": proposicao_id, "descricaoSituacao": None, "erro": f"Interno: {e}"}), 500

# Nova rota para destaques
@app.route("/api/proposicao/<int:id_proposicao>/destaques", methods=["GET"])
def api_destaques(id_proposicao: int):
    res = obter_destaques_emendas(id_proposicao)
    return jsonify({
        "destaques_emendas": [
            {
                "numero": d.numero,
                "autoria": d.autoria,
                "descricao": d.descricao,
                "tipo_destaque": d.tipo_destaque,
                "situacao": d.situacao
            }
            for d in res["destaques_emendas"]
        ]
    })

# Nova rota para pareceres
@app.route("/api/proposicao/<int:id_proposicao>/pareceres", methods=["GET"])
def api_pareceres(id_proposicao: int):
    res = obter_pareceres_substitutivos_votos(id_proposicao)
    return jsonify({
        "pareceres_substitutivos_votos": [
            {
                "tipo_proposicao": p.tipo_proposicao,
                "data_apresentacao": p.data_apresentacao,
                "autor": p.autor,
                "descricao": p.descricao,
                "link_inteiro_teor": p.link_inteiro_teor
            }
            for p in res["pareceres_substitutivos_votos"]
        ]
    })

# Nova rota para procedimentos
@app.route("/api/proposicao/<int:id_proposicao>/procedimentos", methods=["GET"])
def api_procedimentos(id_proposicao: int):
    res = obter_procedimentos_regimentais(id_proposicao)
    return jsonify({
        "procedimentos": [
            {
                "numero": p.numero,
                "autoria": p.autoria,
                "descricao": p.descricao,
                "situacao": p.situacao,
                "data": p.data
            }
            for p in res["procedimentos"]
        ]
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
