# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import win32api
import win32con


def get_process_user():
    """
    Returns the user name of the user running the current process.
    """
    return win32api.GetUserNameEx(win32con.NameSamCompatible)
