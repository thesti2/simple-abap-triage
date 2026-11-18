# Instruction
We are building a Triage system, following is the flow:
1. SAP program will send ABAP dump informaton to xMatters
2. xMatters will trigger AI (please suggest on how to trigger AI, is it via FastAPI backend)
3. AI will check based on the information received, whether the error should be handled by Developer (program bug), Functional (data issue) or Basis (system/resource or connection issue) and also will propose on how to fix. This information will be sent to google chat (if possible).
4. User will be able to interact with the AI via google chat (in a special chat space). User may trigger another sub-agent to fix the issue (in case of Developer or program bug that needs fixing).
5. If a program bug needs fixing, Agent will need to be given the program name and variant on how to simulate the issue. Agent will use the SAP adt MCP server to connect to the system and do the program update.
6. Afthe the program has been updated, the agent will need to provide a test document result to show before and after update with screenshot and provide RCA what the root cause is and what is changed.
7. User will be able to verify the result and give approval if the program's TR can be released and move to UAT environment

*IMPORTANT*: Keep it simple, we start building from a simple MVP that can do the core functionality

# Technology Stack
1. Source system is SAP
2. Incident management / Alert system is xMatters
3. Agent AI can be hosted in GCP (with FastAPI)
4. User and AI interaction is via google chat
5. Langsmith is used for observability


# Phase
## Phase 1: Backend
For each of this ABAP program requested, use the SAP adt tool to connect to the SAP development system and also build a copy of the program in 'ABAP' subfolder. 
1. Build a ABAP program that is purposely has a bug based on certain input. This program will be the sample issue to be used to test this triage system. 
2. Build a program that can be scheduled to read job failure (cancelled state) and collect the information (program name, run time, error message, etc) and send the information to xMatters

## Phase 2: Google Chat Integration
Provide to todo list on how to enable google chat integration to enable the FastAPI framework to forward and receive messages from google chat.

## Phase 3: Agentic AI
Agentic AI will be created with LangGraph and FastAPI. Sending to Google Chat should be deterministic, the alert information should be stored in a persistent database where the AI agent can refer when the user is asking about the issue.
1. Build LangGraph that will receive information from xMatters of the alert and send the alert to google chat
2. On top of the alert information, please add information which system should be handling the issue (Functional, Developer, Basis), root cause analysis and also recommendation on how to fix
3. User will be able to chat with the agent regarding the alert ID, user will be able to decide to fix the program (if development issue) which is triggered via google chat
4. For fixing the issue, send this task to a sub-agent that will use the SAP ADT tool and it will notify to google chat as well when the job is done, together with the RCA and test result document. Update the status of the issue in the persistent db.
5. User will be able to review, and confirm if the fix is complete and Agent would be able to release the TR to move the changes to the next environment

Let me know as well, based on the description above, if we require a langgraph or a langchain create_agent would be suffice

# Information
1. xMatters configuration will be done manaully and seperately, we will inform the webhook url to be used in xMatters

# TODO
Review the task and make a plan (PLAN.md) based on the phases and make a checklist of the success condition on each phases. For each phases, do a thorough testing to ensure the functionality