from .command_tools import execute_command_tool
from .file_tools import (
    edit_file_tool,
    glob_tool,
    grep_tool,
    list_directory_tool,
    read_file_tool,
    write_file_tool,
)
from .plan_tools import plan_tools
from .web_tools import http_request_tool, web_search_tool


# Product tools stay on every request. Schedules, skills, episodes, loops, and
# MCP catalogs pay one discovery round-trip through tool_search.
tools = [
    list_directory_tool,
    glob_tool,
    grep_tool,
    read_file_tool,
    write_file_tool,
    edit_file_tool,
    execute_command_tool,
    *plan_tools,
    web_search_tool,
    http_request_tool,
]
