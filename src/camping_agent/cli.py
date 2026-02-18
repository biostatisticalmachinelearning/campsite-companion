import asyncio
import uuid

from langchain_core.messages import HumanMessage
from langgraph.types import Command
from rich.console import Console
from rich.panel import Panel

from camping_agent.graph import build_graph

console = Console()


async def _process_events(events):
    """Stream agent events and print text responses."""
    async for event in events:
        for node_name, node_output in event.items():
            if node_name == "agent":
                msgs = node_output.get("messages", [])
                for msg in msgs:
                    if (
                        hasattr(msg, "content")
                        and msg.content
                        and not getattr(msg, "tool_calls", None)
                    ):
                        console.print(f"\n[bold green]Agent:[/] {msg.content}\n")


async def _handle_interrupts(graph, config):
    """Check for and handle any interrupt (human-in-the-loop) points."""
    snapshot = await graph.aget_state(config)
    while snapshot.next:
        # Show interrupt prompts
        for task in snapshot.tasks:
            if hasattr(task, "interrupts") and task.interrupts:
                for intr in task.interrupts:
                    console.print(f"\n[bold yellow]{intr.value}[/]")

        response = console.input("[bold cyan]You:[/] ")
        events = graph.astream(
            Command(resume=response),
            config,
            stream_mode="updates",
        )
        await _process_events(events)
        snapshot = await graph.aget_state(config)


async def _run_loop(graph, config):
    """Main conversation loop."""
    while True:
        user_input = console.input("[bold cyan]You:[/] ")
        if user_input.strip().lower() in ("quit", "exit", "q"):
            console.print("Goodbye!")
            break

        events = graph.astream(
            {"messages": [HumanMessage(content=user_input)]},
            config,
            stream_mode="updates",
        )
        await _process_events(events)
        await _handle_interrupts(graph, config)


def main():
    console.print(
        Panel(
            "[bold]Campsite Companion[/]\n"
            "Find and book campsites across California.\n"
            'Type "quit" to exit.',
            style="green",
        )
    )
    console.print(
        "Tell me what kind of camping you're looking for!\n"
        "Example: [dim]Find me a campsite near Yosemite for next weekend, "
        "2 people[/]\n"
    )

    graph = build_graph()
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    asyncio.run(_run_loop(graph, config))
