import os
import time
from poller import LocalPoller
from make_agents import AgentsMaker
import json
from dotenv import load_dotenv

PATH= os.environ.get("INCOMING_PATH", f"{os.path.dirname(os.path.abspath(__file__))}/incoming")

def main():
    load_dotenv()  # Carica le variabili d'ambiente dal file .env
    print(f"Creo Agenti Azure AI Foundry con modello: {os.environ.get('AZ_MODEL_NAME', 'gpt-4.1-mini')}")
    agent_maker = AgentsMaker()
    # agent_maker.make_agents()
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
    #Call Agent 



if __name__ == "__main__":
    load_dotenv()  # Carica le variabili d'ambiente dal file .env
    main()