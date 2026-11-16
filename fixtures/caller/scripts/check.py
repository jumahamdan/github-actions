"""A synthetic caller with an actual assertion and a retained build output."""
from pathlib import Path
import xml.etree.ElementTree as ET

message=b"verified shared workflow fixture\n"
assert message.decode().strip()=="verified shared workflow fixture"
output=Path("build");output.mkdir(exist_ok=True)
(output/"app.bin").write_bytes(message)
suite=ET.Element("testsuite",tests="1",failures="0",errors="0")
ET.SubElement(suite,"testcase",name="synthetic payload matches expected content")
ET.ElementTree(suite).write(output/"tests.xml",encoding="unicode")
