import os
import json
import sys
from azure.ai.agents import AgentsClient
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from azure.ai.agents.models import (
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

        # Sotto-Agente 2: usa la knowledge base locale caricata da data/articoli.csv
        kbase_agent = agents_client.create_agent(
            model=self.model_name,
            name="Articoli-KBase-Agent",
            instructions=self._build_kbase_instructions(articoli_csv_text, articoli_source_name),
        )
        created["agents"]["articoli_kbase"] = kbase_agent.id

        client_agent = agents_client.create_agent(
            model=self.model_name,
            name="Clients-KBase-Agent",
            instructions=self._build_kbase_instructions(clients_csv_text, clients_source_name),
        )
        created["agents"]["clients_kbase"] = client_agent.id

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

        supervisor_agent = agents_client.create_agent(
            model=self.model_name,
            name="Kbase-Api-Workflow-Manager",
            instructions=(
                "Sei il manager del flusso. In base alla domanda dell'utente:\n"
                "1. Identifitica il cliente attraverso l'agente 'Clients_KBase_Agent'.\n"
                "2. Controlla i dati attraverso l'agente 'Json-vgest-Agent'.\n"
                "3. Identifica se è una domanda o un ordine relativo agli articoli'.\n"
                "4. Indentifica gli articoli attraverso l'agente 'Articoli_KBase_Agent'.\n"
                "5. controlla se l'articolo è disponibile e il prezzo attraverso l'agente 'Json-vgest-Agent'.\n"
                "6. Se è una domanda prepara una risposta chiara e completa basata solo su quanto ottenuto dai sotto-agenti.\n"
                "7. Se è un ordine, prepara una risposta chiara e completa basata solo su quanto ottenuto dai sotto-agenti e includi le informazioni necessarie per completare l'ordine.\n"
                "8. Prepara un documento JSON secondo il seguente formato.\n" 
                """{
                    'cliente': {dati_del_cliente_da_vgest},
                    'tipologia_richiesta': <domanda_o_ordine>,
                    'articoli':[{articoli da vgest o kbase}, note: <note ricevute dal cliente>],
                    'Testo": <testo_risposta_chiara_e_completa>,
                    'risposta': <risposta_chiara_e_completa>
                }\n"""
                "3. Se la domanda riguarda clienti, chiama l'agente 'Clients_KBase_Agent'.\n"
                "4. Se la domanda richiede piu fonti, chiama gli agenti necessari e combina le risposte.\n"
                "Restituisci una risposta chiara basata solo su quanto ottenuto dai sotto-agenti."
            ),
            tools=api_tool.definitions + kbase_tool.definitions + clients_tool.definitions
        )
        created["agents"]["supervisor"] = supervisor_agent.id

        self._save_state(created)
        print(f"Creati agent e file. Stato salvato in: {self._state_path()}")
        return supervisor_agent

    def delete_agents(self):
        agents_client = self._build_clients()
        state = self._load_state()

        deleted = {"agents": [], "files": []}

        for _, agent_id in state.get("agents", {}).items():
            try:
                agents_client.delete_agent(agent_id)
                deleted["agents"].append(agent_id)
            except Exception as ex:
                print(f"Impossibile cancellare agent {agent_id}: {ex}")

        for _, file_id in state.get("files", {}).items():
            try:
                agents_client.files.delete(file_id)
                deleted["files"].append(file_id)
            except Exception as ex:
                print(f"Impossibile cancellare file {file_id}: {ex}")

        if os.path.exists(self._state_path()):
            os.remove(self._state_path())

        print("Cancellazione completata.")
        return deleted
