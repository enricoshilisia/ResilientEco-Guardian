import json
from channels.generic.websocket import AsyncWebsocketConsumer
from asgiref.sync import sync_to_async
from .agents.core_agents import run_all_agents

KENYA_CITIES = {
    'nairobi': (-1.2921, 36.8219),
    'mombasa': (-4.0435, 39.6682),
    'kisumu': (-0.0917, 34.7680),
    'nakuru': (-0.3031, 36.0800),
    'eldoret': (0.5143, 35.2698),
    'kakamega': (0.2827, 34.7519),
    'kitale': (1.0157, 35.0062),
    'thika': (-1.0332, 37.0690),
    'malindi': (-3.2167, 40.1167),
    'kisii': (-0.6817, 34.7667),
    'nyeri': (-0.4167, 36.9500),
}

class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        await self.accept()
        await self.send(text_data=json.dumps({
            'message': '✅ Connected to ResilientEco Guardian multi-agent system.',
            'type': 'system'
        }))

    async def disconnect(self, close_code):
        pass

    async def receive(self, text_data):
        data = json.loads(text_data)
        message = data['message'].lower()

        await self.send(text_data=json.dumps({
            'message': '🔄 Running 5-agent analysis...',
            'type': 'thinking'
        }))

        # Detect city
        detected_city = ('nairobi', (-1.2921, 36.8219))
        for city, coords in KENYA_CITIES.items():
            if city in message:
                detected_city = (city, coords)
                break

        city_name, (lat, lon) = detected_city

        # Run all 5 agents
        results = await sync_to_async(run_all_agents)(
            data['message'], lat, lon, city_name.title()
        )

        # Handle error
        if 'error' in results:
            await self.send(text_data=json.dumps({
                'message': f"⚠️ Agent error: {results['error']}",
                'type': 'error'
            }))
            return

        # Agent icons
        agent_icons = {
            'monitor': '🔍 Monitor Agent',
            'predict': '📊 Predict Agent',
            'decision': '🧠 Decision Agent',
            'action': '⚡ Action Agent',
            'governance': '⚖️ Governance Agent'
        }

        # Send each agent result separately
        for agent, output in results.items():
            icon = agent_icons.get(agent, f'🤖 {agent}')
            await self.send(text_data=json.dumps({
                'message': f"{icon}:\n{output}",
                'type': agent
            }))

        await self.send(text_data=json.dumps({
            'message': f"✅ Analysis complete for {city_name.title()}.",
            'type': 'complete'
        }))