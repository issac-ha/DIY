"""
Hardware Agent
硬件价格分析 Agent
把 LLM 和工具组合起来,让 LLM 可以调用工具,并且可以使用工具的结果来回答问题
"""
from langchain.agents import create_agent
from app.llm_client import get_llm
from app.tools.hardware_tools import TOOLS
#获取 LLM 客户端
llm = get_llm() 

#创建Agent: LLM + 工具

agent = create_agent(
    model=llm,
    tools=TOOLS,
    system_prompt=(
        "你是一个专业的硬件价格分析助手。"
        "数据来源是B站硬件资讯视频（含视频画面里的价格表识别）和数据库中的历史价格。"
        "用户问价格、趋势、性价比时，先调用工具获取真实数据再回答，不要编造价格；"
        "用户问最近行情或视频内容时，调用 get_video_summary 查看视频总结。"
    )
)