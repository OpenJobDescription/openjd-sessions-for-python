# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from ._os_checker import is_windows

if is_windows():
    import win32security
    import ntsecuritycon


class WindowsPermissionHelper:
    """
    This class contains helper methods to set permissions for files and directories on Windows.
    """

    @staticmethod
    def set_permissions(
        file_path: str,
        *,
        principals_full_control: list[str] = [],
        principals_modify_access: list[str] = [],
    ):
        """
        Grants access control over the object at file_path.
        Sets flags so both child files and directories inherit these permissions.

        Arguments:
            file_path (str): The path to the file or directory.
            principals_full_control (List[str]): The names of the principals to permit Full Control access.
            principals_modify_access (List[str]): The names of the principals to permit Modify access

        Raises:
            RuntimeError if there is a problem modifying the security attributes.
        """
        try:
            # We don't want to propagate existing permissions, so create a new DACL
            dacl = win32security.ACL()
            for principal in principals_full_control:
                user_or_group_sid, _, _ = win32security.LookupAccountName(None, principal)

                # Add an ACE to the DACL giving the principal full control and enabling inheritance of the ACE
                dacl.AddAccessAllowedAceEx(
                    win32security.ACL_REVISION,
                    ntsecuritycon.OBJECT_INHERIT_ACE | ntsecuritycon.CONTAINER_INHERIT_ACE,
                    ntsecuritycon.FILE_ALL_ACCESS,  # = 0x1F01FF
                    user_or_group_sid,
                )
            for principal in principals_modify_access:
                user_or_group_sid, _, _ = win32security.LookupAccountName(None, principal)

                # Add an ACE to the DACL giving the principal full control and enabling inheritance of the ACE
                dacl.AddAccessAllowedAceEx(
                    win32security.ACL_REVISION,
                    ntsecuritycon.OBJECT_INHERIT_ACE | ntsecuritycon.CONTAINER_INHERIT_ACE,
                    # Values of these constants defined in winnt.h
                    # Constant value after ORs: 0x1301FF
                    # Delta from FILE_ALL_ACCESS is 0xC0000 = WRITE_DAC(0x40000) | WRITE_OWNER(0x80000)
                    ntsecuritycon.FILE_GENERIC_READ  # = 0x120089
                    | ntsecuritycon.FILE_GENERIC_WRITE  # = 0x120116
                    | ntsecuritycon.FILE_GENERIC_EXECUTE  # = 0x1200A0
                    | ntsecuritycon.DELETE  # = 0x10000
                    | ntsecuritycon.FILE_DELETE_CHILD,  # = 0x0040
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
            # Note: This completely overwrites the DACL; so, if we don't provide a permission above then
            # the DACL doesn't have it.
            win32security.SetFileSecurity(
                str(file_path), win32security.DACL_SECURITY_INFORMATION, sd
            )
        except Exception as err:
            raise RuntimeError(
                f"Could not change permissions of directory '{str(dir)}' (error: {str(err)})"
            )
