# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import re

h2 = r"^##\sv.*$"
with open("CHANGELOG.md") as f:
    contents = f.read()
matches = re.findall(h2, contents, re.MULTILINE)
changelog = contents[: contents.find(matches[1]) - 1] if len(matches) > 1 else contents
print(changelog)
