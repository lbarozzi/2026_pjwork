import os
import json
import sys
from azure.ai.agents import AgentsClient
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from azure.ai.agents.models import (
    CodeInterpreterTool,
    ConnectedAgentTool,
    FileSearchTool,
    ListSortOrder,
    OpenApiTool,
    OpenApiAnonymousAuthDetails,
)

class AgentsMaker:
    def __init__(self, model_name=None):
        self.model_name = model_name or os.environ.get("AZ_MODEL_NAME", "gpt-4.1-mini")
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        

    def _state_path(self):
        return os.path.join(self.base_dir, "data", "agents_state.json")

    def _resolve_data_file(self, *candidate_names):
        for name in candidate_names:
            path = os.path.join(self.base_dir, "data", name)
            if os.path.exists(path):
                return path
        raise FileNotFoundError(f"Nessun file trovato tra: {', '.join(candidate_names)}")

    def _load_data_text(self, *candidate_names):
        path = self._resolve_data_file(*candidate_names)
        with open(path, "r", encoding="utf-8") as f:
            return f.read(), os.path.basename(path)

    def _build_kbase_instructions(self, csv_text, source_name):
        return (
            "Sei un agente che risponde esclusivamente basandosi sui contenuti del file locale "
            f"{source_name}. Non inventare dati e non usare conoscenza esterna.\n\n"
            f"Contenuto disponibile:\n{csv_text}\n\n"
            "Se l'informazione non è presente nel file, dichiaralo esplicitamente."
        )

    def _build_clients(self):
        credential = DefaultAzureCredential()
        agents_client = AgentsClient(
            endpoint=os.environ["AZ_FOUNDRY_EP"],
            credential=credential
        )
        AIProjectClient(
            endpoint=os.environ["AZ_FOUNDRY_EP"],
            credential=credential
        )
        self.agents_client = agents_client
        return agents_client

    def _save_state(self, state):
        with open(self._state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=True)

    def _load_state(self):
        with open(self._state_path(), "r", encoding="utf-8") as f:
            return json.load(f)

    def make_agents(self):
        # 1. Connessione al progetto Azure AI Foundry
        # Assicurati di impostare la variabile d'ambiente: os.environ["AZ_FOUNDRY_EP"]
        agents_client = self._build_clients()

        print("--- 2. Creazione degli Agenti Specializzati (Sub-Agents) ---")

        # Sotto-Agente 1: chiama una API REST tramite OpenApiTool
        # Usiamo come servizio un'API REST java aziendale
        # Spec OpenAPI 3.0 minimale scritta a mano per i due endpoint usati dalla demo
        json_spec = {}
        with open(os.path.join(self.base_dir, "data", "openapi.json"), "r", encoding="utf-8") as f:
            json_spec = json.load(f)

        articoli_csv_text, articoli_source_name = self._load_data_text("articoli.csv")
        clients_csv_text, clients_source_name = self._load_data_text("clients.csv", "clienti.csv")

        created = {
            "model": self.model_name,
            "files": {},
            "agents": {}
        }

        openapi_tool = OpenApiTool(
            name="Json_vgest_openApi",
            description="Interroga l'API REST java aziendale per recuperare post di esempio.",
            spec=json_spec,
            auth=OpenApiAnonymousAuthDetails()
        )

        api_agent = agents_client.create_agent(
            model=self.model_name,
            name="Json-vgest-Agent",
            instructions=(
                "Sei un agente che risponde alle domande chiamando l'API REST. "
                "Usa lo strumento disponibile per recuperare i post "
                "richiesti prima di rispondere."
            ),
            tools=openapi_tool.definitions
        )
        created["agents"]["api"] = api_agent.id
        self._save_state(created)

        # Sotto-Agente 2: usa la knowledge base locale caricata da data/articoli.csv
        kbase_agent = agents_client.create_agent(
            model=self.model_name,
            name="Articoli-KBase-Agent",
            instructions=self._build_kbase_instructions(articoli_csv_text, articoli_source_name),
        )
        created["agents"]["articoli_kbase"] = kbase_agent.id
        self._save_state(created)

        client_agent = agents_client.create_agent(
            model=self.model_name,
            name="Clients-KBase-Agent",
            instructions=self._build_kbase_instructions(clients_csv_text, clients_source_name),
        )
        created["agents"]["clients_kbase"] = client_agent.id
        self._save_state(created)

        print("--- 3. Creazione dell'Agente Principale (Orchestratore) ---")

        # Registriamo i sub-agent come "Connected Agents": l'orchestratore potrà
        # richiamarli come fossero strumenti nativi
        api_tool = ConnectedAgentTool(
            id=api_agent.id,
            name="JsonPlaceholder_Api_Agent",
            description="Chiama l'API REST aziendale per recuperare dati aggiornati."
        )
        kbase_tool = ConnectedAgentTool(
            id=kbase_agent.id,
            name="Articoli_KBase_Agent",
            description="Risponde a domande usando i contenuti del file locale data/articoli.csv."
        )
        clients_tool = ConnectedAgentTool(
            id=client_agent.id,
            name="Clients_KBase_Agent",
            description="Risponde a domande usando i contenuti del file locale data/clienti.csv."
        )

        # Permette al supervisor di leggere i PDF allegati ai messaggi (es. ordini ricevuti)
        code_interpreter_tool = CodeInterpreterTool()

        supervisor_agent = agents_client.create_agent(
            model=self.model_name,
            name="Kbase-Api-Workflow-Manager",
            instructions=(
                """Sei l'orchestratore di un sistema multi-agente per l'elaborazione di richieste commerciali B2B (ordini di acquisto e domande informative) ricevute via PDF, email o form. Il tuo compito è coordinare i sotto-agenti, estrarre e validare i dati, segnalare anomalie e produrre un output strutturato per la revisione umana.

## Sotto-agenti disponibili
- `Clients_KBase_Agent`: anagrafica clienti dalla knowledge base.
- `Json-vgest-Agent`: dati gestionali da VGest (clienti, codici articolo, disponibilità, listino prezzi).
- `Articoli_KBase_Agent`: catalogo articoli (descrizioni, varianti, specifiche tecniche).

## Flusso operativo

### Fase 1 — Estrazione e classificazione
Analizza il documento in ingresso ed estrai:
- Tipologia: `domanda` (richiesta informativa) oppure `ordine` (intento di acquisto esplicito).
- Metadati: riferimento cliente, data documento, data consegna richiesta, indirizzo di consegna, condizioni di pagamento, riferimenti a offerte o accordi precedenti.
- Note libere a livello di ordine e a livello di singolo articolo.

### Fase 2 — Identificazione cliente
1. Estrai ragione sociale, P.IVA, indirizzo, contatti dal documento.
2. Chiama `Clients_KBase_Agent` per identificare il cliente.
3. Verifica e arricchisci i dati con `Json-vgest-Agent` (codice cliente VGest, fido, condizioni quadro).
4. Se il cliente non è identificabile univocamente o ha dati incoerenti (es. P.IVA non trovata, indirizzo diverso da quello in anagrafica), imposta `cliente.match: "ambiguo"` o `"non_trovato"` e descrivi il problema in `review_notes`.

### Fase 3 — Validazione articoli (solo per richieste con codici/descrizioni articolo)
Per ciascun articolo del documento:
1. Chiama `Articoli_KBase_Agent` con il codice (es. `ART-015`) e la descrizione del cliente.
2. Chiama `Json-vgest-Agent` per recuperare disponibilità a magazzino e prezzo di listino.
3. Confronta il prezzo richiesto dal cliente con il prezzo VGest:
   - Scostamento < 1%: `prezzo_match: "ok"`.
   - Scostamento ≥ 1%: `prezzo_match: "discrepanza"` e aggiungi una voce in `review_notes`.
4. Se il cliente specifica una variante o requisito (es. "curva C", "cavo 5m", "certificato X"), copialo in `note_articolo` e imposta `richiede_verifica_tecnica: true`.
5. Se l'articolo non esiste in VGest o in KBase, imposta `match: "non_trovato"` e segnala in `review_notes`.

### Fase 4 — Anomalie da segnalare sempre
Aggiungi una voce in `review_notes` per ognuno di questi casi:
- Riferimenti a accordi verbali o telefonici ("come da accordi con il sig. X").
- Date di consegna inferiori a 5 giorni lavorativi (urgenza).
- Condizioni di pagamento diverse da quelle in anagrafica cliente.
- Importo totale dell'ordine superiore al fido residuo del cliente (se disponibile in VGest).
- Alternative accettate dal cliente ("se non disponibile X, accettiamo Y").

### Fase 5 — Composizione risposta
- Per `domanda`: risposta sintetica con i dati richiesti.
- Per `ordine`: riepilogo conferma (cliente, articoli validati, totale calcolato, data consegna proposta) e indicazione dei punti aperti che richiedono intervento umano.

## Vincoli
- **Mai inventare dati**: se un'informazione non è restituita dai sotto-agenti, lascia `null` e segnalalo.
- **Mai modificare i dati del cliente**: riporta quanto scritto nel documento e separatamente quanto risulta in VGest/KBase; il confronto è esplicito.
- **Lingua di risposta**: italiano.
- **Determinismo**: a parità di input, l'output JSON deve essere identico.
- **Autonomia**: non richiedere input umano durante l'elaborazione; tutte le decisioni devono essere prese dai sotto-agenti o segnalate in `review_notes`.

## Formato di output (obbligatorio)
Restituisci un unico oggetto JSON valido secondo questo schema:

{
  "tipologia_richiesta": "domanda | ordine",
  "metadati_documento": {
    "riferimento_cliente": "<string_o_null>",
    "data_documento": "<YYYY-MM-DD_o_null>",
    "data_consegna_richiesta": "<YYYY-MM-DD_o_null>",
    "indirizzo_consegna": "<string_o_null>",
    "condizioni_pagamento": "<string_o_null>",
    "riferimenti_precedenti": "<string_o_null>"
  },
  "cliente": {
    "match": "ok | ambiguo | non_trovato",
    "codice_vgest": "<string_o_null>",
    "ragione_sociale_dichiarata": "<string>",
    "ragione_sociale_vgest": "<string_o_null>",
    "piva": "<string_o_null>",
    "dati_vgest": { /* fido, condizioni quadro, contatti */ }
  },
  "articoli": [
    {
      "codice_dichiarato": "<string>",
      "descrizione_dichiarata": "<string>",
      "codice_vgest": "<string_o_null>",
      "descrizione_vgest": "<string_o_null>",
      "match": "ok | ambiguo | non_trovato",
      "quantita": <number>,
      "unita_misura": "<string>",
      "prezzo_dichiarato": <number_o_null>,
      "prezzo_vgest": <number_o_null>,
      "prezzo_match": "ok | discrepanza | non_verificabile",
      "disponibile": <boolean_o_null>,
      "giacenza": <number_o_null>,
      "note_articolo": "<string_o_null>",
      "richiede_verifica_tecnica": <boolean>
    }
  ],
  "totali": {
    "importo_dichiarato": <number_o_null>,
    "importo_calcolato_vgest": <number_o_null>,
    "totale_match": "ok | discrepanza | non_verificabile"
  },
  "note_cliente": "<note_generali_dell_ordine_o_null>",
  "review_notes": [
    {
      "tipo": "prezzo_discrepante | accordo_verbale | urgenza_consegna | pagamento_non_standard | articolo_non_trovato | cliente_ambiguo | alternativa_accettata | fido_superato | altro",
      "riferimento": "<articolo_o_sezione>",
      "dettaglio": "<descrizione_per_revisore_umano>"
    }
  ],
  "risposta": "<testo_finale_per_l_utente>"
}
"""
            ),
            tools=api_tool.definitions + kbase_tool.definitions + clients_tool.definitions
            + code_interpreter_tool.definitions
        )
        created["agents"]["supervisor"] = supervisor_agent.id

        
        self._save_state(created)
        print(f"Creati agent e file. Stato salvato in: {self._state_path()}")
        return supervisor_agent

    def delete_agents(self):
        agents_client = self._build_clients()
        state = self._load_state()

        deleted = {"agents": [], "files": []}

        # Cancella prima l'orchestratore, poi i sub-agent che referenzia
        # come ConnectedAgentTool, per evitare riferimenti pendenti.
        agent_items = list(state.get("agents", {}).items())
        agent_items.sort(key=lambda item: item[0] != "supervisor")

        remaining_agents = {}
        for name, agent_id in agent_items:
            try:
                agents_client.delete_agent(agent_id)
                deleted["agents"].append(agent_id)
            except Exception as ex:
                print(f"Impossibile cancellare agent {agent_id}: {ex}")
                remaining_agents[name] = agent_id

        remaining_files = {}
        for name, file_id in state.get("files", {}).items():
            try:
                agents_client.files.delete(file_id)
                deleted["files"].append(file_id)
            except Exception as ex:
                print(f"Impossibile cancellare file {file_id}: {ex}")
                remaining_files[name] = file_id

        if remaining_agents or remaining_files:
            state["agents"] = remaining_agents
            state["files"] = remaining_files
            self._save_state(state)
            print(
                "Cancellazione parziale: alcuni agent/file non sono stati rimossi e "
                f"restano tracciati in {self._state_path()} per un nuovo tentativo."
            )
        else:
            if os.path.exists(self._state_path()):
                os.remove(self._state_path())
            print("Cancellazione completata.")

        return deleted
