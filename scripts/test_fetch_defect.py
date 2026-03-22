import asyncio
import sys
sys.path.insert(0, '.')
from a2a_models import Message, Part, Role
import main

async def run():
    part = Part(data={"tool":"get_defect", "entityId":1315})
    msg = Message(role=Role.USER, parts=[part], contextId='ctx-test')
    res = await main._handle_with_keywords('tid-test','ctx-test','', msg, None)
    print(res)

if __name__ == '__main__':
    asyncio.run(run())
