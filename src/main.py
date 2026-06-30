import os
import time
from poller import LocalPoller
from make_agents import AgentsMaker
from azure.ai.agents.models import CodeInterpreterTool, FilePurpose, MessageAttachment
import json
from dotenv import load_dotenv

PATH= os.environ.get("INCOMING_PATH", f"{os.path.dirname(os.path.abspath(__file__))}/incoming")
agent_maker = None
supervisor_agent = None

def main():
    global agent_maker, supervisor_agent
    load_dotenv()  # Carica le variabili d'ambiente dal file .env
    print(f"Creo Agenti Azure AI Foundry con modello: {os.environ.get('AZ_MODEL_NAME', 'gpt-4.1-mini')}")
    agent_maker = AgentsMaker()
    supervisor_agent =agent_maker.make_agents()
    print(f"Starting poller on {PATH}")
    lc_poller = LocalPoller(process=process_file, path=PATH, interval_seconds=5)
    lc_poller.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping poller...")
        lc_poller.stop()
    finally:
        print(f"Clean azure resources and exit.")
        agent_maker.delete_agents()

    print("End of workflow.")


def process_file(file_path):
    print(f"Process_file: {file_path}")

    if supervisor_agent is None:
        print("Supervisor agent non disponibile, impossibile processare il file.")
        return

    agents_client = agent_maker.agents_client

    uploaded_file = agents_client.files.upload(
        file_path=file_path,
        purpose=FilePurpose.AGENTS,
        filename=os.path.basename(file_path),
    )

    try:
        thread = agents_client.threads.create()
        agents_client.messages.create(
            thread_id=thread.id,
            role="user",
            content="Analizza il file allegato.",
            attachments=[
                MessageAttachment(
                    file_id=uploaded_file.id,
                    tools=CodeInterpreterTool().definitions,
                )
            ],
        )
        agents_client.runs.create_and_process(thread_id=thread.id, agent_id=supervisor_agent.id)

        last_message = agents_client.messages.get_last_message_text_by_role(thread.id, "assistant")
        if last_message:
            print(f"Risposta supervisor agent:\n{last_message.text.value}")
        else:
            print("Nessuna risposta ricevuta dal supervisor agent.")
    finally:
        agents_client.files.delete(uploaded_file.id)



if __name__ == "__main__":
    load_dotenv()  # Carica le variabili d'ambiente dal file .env
    main()