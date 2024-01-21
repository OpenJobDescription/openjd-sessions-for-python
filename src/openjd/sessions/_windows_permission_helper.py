# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from ._os_checker import is_windows

if is_windows():
    import win32security
    import ntsecuritycon
    import win32api
    import win32con


class WindowsPermissionHelper:
    """
    This class contains helper methods to set permissions for files and directories on Windows.
    """

    @staticmethod
    def set_permissions_full_control(file_path, principals_to_permit):
        try:
            # We don't want to propagate existing permissions, so create a new DACL
            dacl = win32security.ACL()
            for principal in principals_to_permit:
                user_or_group_sid, _, _ = win32security.LookupAccountName(None, principal)

                # Add an ACE to the DACL giving the principal full control and enabling inheritance of the ACE
                dacl.AddAccessAllowedAceEx(
                    win32security.ACL_REVISION,
                    ntsecuritycon.OBJECT_INHERIT_ACE | ntsecuritycon.CONTAINER_INHERIT_ACE,
                    ntsecuritycon.FILE_ALL_ACCESS,
                    user_or_group_sid,
                )

            # Get the security descriptor of the tempdir
            sd = win32security.GetFileSecurity(
                str(file_path), win32security.DACL_SECURITY_INFORMATION
            )

            # Set the security descriptor's DACL to the newly-created DACL
            # Arguments:
            # 1. bDaclPresent = 1: Indicates that the DACL is present in the security descriptor.
            #    If set to 0, this method ignores the provided DACL and allows access to all principals.
            # 2. dacl: The discretionary access control list (DACL) to be set in the security descriptor.
            # 3. bDaclDefaulted = 0: Indicates the DACL was provided and not defaulted.
            #    If set to 1, indicates the DACL was defaulted, as in the case of permissions inherited from a parent directory.
            sd.SetSecurityDescriptorDacl(1, dacl, 0)

            # Set the security descriptor to the tempdir
            win32security.SetFileSecurity(
                str(file_path), win32security.DACL_SECURITY_INFORMATION, sd
            )
        except Exception as err:
            raise RuntimeError(
                f"Could not change permissions of directory '{str(dir)}' (error: {str(err)})"
            )

    @staticmethod
    def get_process_user():
        return win32api.GetUserNameEx(win32con.NameSamCompatible)
