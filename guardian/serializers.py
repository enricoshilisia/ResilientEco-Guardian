"""
ResilientEco Guardian - Account Serializers
DRF serializers for end-user account management
"""

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken

from .models import (
    Organization, OrganizationMembership, UserProfile,
    OrganizationInvitation, SavedLocation, AccountActivityLog
)


# ─────────────────────────────────────────────
# REGISTRATION & AUTH
# ─────────────────────────────────────────────

class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(
        write_only=True,
        required=True,
        validators=[validate_password]
    )
    password2 = serializers.CharField(write_only=True, required=True)
    is_public_user = serializers.BooleanField(default=False)

    class Meta:
        model = User
        fields = ('username', 'email', 'first_name', 'last_name',
                  'password', 'password2', 'is_public_user')

    def validate(self, attrs):
        if attrs['password'] != attrs['password2']:
            raise serializers.ValidationError({"password": "Passwords do not match."})
        if User.objects.filter(email=attrs['email']).exists():
            raise serializers.ValidationError({"email": "Email already registered."})
        return attrs

    def create(self, validated_data):
        is_public = validated_data.pop('is_public_user', False)
        validated_data.pop('password2')

        user = User.objects.create_user(**validated_data)

        # Profile is auto-created via signal; update public flag
        user.profile.is_public_user = is_public
        user.profile.save()

        return user


class RegisterResponseSerializer(serializers.Serializer):
    """Response after successful registration."""
    user_id = serializers.IntegerField()
    username = serializers.CharField()
    email = serializers.CharField()
    access = serializers.CharField()
    refresh = serializers.CharField()
    message = serializers.CharField()


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(required=True)
    password = serializers.CharField(required=True, write_only=True)


# ─────────────────────────────────────────────
# USER PROFILE
# ─────────────────────────────────────────────

class UserMiniSerializer(serializers.ModelSerializer):
    """Lightweight user representation for embedding."""
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'full_name')

    def get_full_name(self, obj):
        return obj.get_full_name() or obj.username


class UserProfileSerializer(serializers.ModelSerializer):
    user = UserMiniSerializer(read_only=True)
    organizations = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = UserProfile
        fields = (
            'user', 'phone', 'bio', 'avatar', 'avatar_url',
            'timezone', 'language', 'notifications_enabled',
            'notification_channel', 'alert_threshold',
            'is_public_user', 'is_verified_email',
            'last_active', 'login_count',
            'organizations', 'created_at', 'updated_at'
        )
        read_only_fields = (
            'is_verified_email', 'last_active', 'login_count',
            'created_at', 'updated_at'
        )

    def get_organizations(self, obj):
        memberships = OrganizationMembership.objects.filter(
            user=obj.user, is_active=True
        ).select_related('organization')
        return [
            {
                'id': str(m.organization.id),
                'name': m.organization.name,
                'org_type': m.organization.org_type,
                'role': m.role,
                'is_default': (
                    obj.default_organization_id == m.organization.id
                )
            }
            for m in memberships
        ]

    def get_avatar_url(self, obj):
        if obj.avatar:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.avatar.url)
        return None


class UpdateProfileSerializer(serializers.ModelSerializer):
    first_name = serializers.CharField(source='user.first_name', required=False)
    last_name = serializers.CharField(source='user.last_name', required=False)
    email = serializers.EmailField(source='user.email', required=False)

    class Meta:
        model = UserProfile
        fields = (
            'first_name', 'last_name', 'email',
            'phone', 'bio', 'avatar',
            'timezone', 'language',
            'notifications_enabled', 'notification_channel',
            'alert_threshold', 'default_organization'
        )

    def validate_email(self, value):
        user = self.context['request'].user
        if User.objects.exclude(pk=user.pk).filter(email=value).exists():
            raise serializers.ValidationError("Email already in use.")
        return value

    def update(self, instance, validated_data):
        # Handle nested user fields
        user_data = validated_data.pop('user', {})
        user = instance.user
        for attr, val in user_data.items():
            setattr(user, attr, val)
        user.save()

        # Update profile fields
        for attr, val in validated_data.items():
            setattr(instance, attr, val)
        instance.save()
        return instance


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(required=True, write_only=True)
    new_password = serializers.CharField(
        required=True,
        write_only=True,
        validators=[validate_password]
    )
    new_password2 = serializers.CharField(required=True, write_only=True)

    def validate(self, attrs):
        if attrs['new_password'] != attrs['new_password2']:
            raise serializers.ValidationError({"new_password": "Passwords do not match."})
        return attrs

    def validate_old_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError("Old password is incorrect.")
        return value


# ─────────────────────────────────────────────
# ORGANIZATION
# ─────────────────────────────────────────────

class OrganizationSerializer(serializers.ModelSerializer):
    member_count = serializers.SerializerMethodField()
    current_user_role = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = (
            'id', 'name', 'slug', 'org_type', 'country', 'region',
            'logo', 'website', 'description', 'is_active', 'is_verified',
            'member_count', 'current_user_role', 'created_at'
        )
        read_only_fields = ('id', 'slug', 'is_verified', 'created_at')

    def get_member_count(self, obj):
        return obj.members.filter(is_active=True).count()

    def get_current_user_role(self, obj):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return None
        try:
            m = OrganizationMembership.objects.get(
                user=request.user, organization=obj, is_active=True
            )
            return m.role
        except OrganizationMembership.DoesNotExist:
            return None

    def create(self, validated_data):
        from django.utils.text import slugify
        name = validated_data.get('name', '')
        slug = slugify(name)
        # Ensure slug uniqueness
        base = slug
        n = 1
        while Organization.objects.filter(slug=slug).exists():
            slug = f"{base}-{n}"
            n += 1
        validated_data['slug'] = slug
        return super().create(validated_data)


class OrganizationMembershipSerializer(serializers.ModelSerializer):
    user = UserMiniSerializer(read_only=True)
    organization_name = serializers.CharField(source='organization.name', read_only=True)

    class Meta:
        model = OrganizationMembership
        fields = (
            'id', 'user', 'organization', 'organization_name',
            'role', 'joined_at', 'is_active', 'invited_by'
        )
        read_only_fields = ('id', 'joined_at', 'invited_by')


class UpdateMemberRoleSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=OrganizationMembership.ROLE_CHOICES)


# ─────────────────────────────────────────────
# INVITATIONS
# ─────────────────────────────────────────────

class InvitationSerializer(serializers.ModelSerializer):
    invited_by_name = serializers.CharField(
        source='invited_by.get_full_name', read_only=True
    )
    organization_name = serializers.CharField(
        source='organization.name', read_only=True
    )
    is_expired = serializers.BooleanField(read_only=True)
    is_valid = serializers.BooleanField(read_only=True)

    class Meta:
        model = OrganizationInvitation
        fields = (
            'id', 'organization', 'organization_name',
            'email', 'role', 'status',
            'invited_by', 'invited_by_name',
            'created_at', 'expires_at',
            'is_expired', 'is_valid'
        )
        read_only_fields = (
            'id', 'token', 'status', 'invited_by',
            'created_at', 'expires_at'
        )


class CreateInvitationSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)
    role = serializers.ChoiceField(
        choices=OrganizationMembership.ROLE_CHOICES,
        default='viewer'
    )

    def validate_email(self, value):
        organization = self.context.get('organization')
        if organization:
            # Check if already a member
            if OrganizationMembership.objects.filter(
                user__email=value,
                organization=organization,
                is_active=True
            ).exists():
                raise serializers.ValidationError(
                    "This user is already a member of the organization."
                )
            # Check if pending invite exists
            if OrganizationInvitation.objects.filter(
                email=value,
                organization=organization,
                status='pending'
            ).exists():
                raise serializers.ValidationError(
                    "A pending invitation for this email already exists."
                )
        return value


# ─────────────────────────────────────────────
# SAVED LOCATIONS
# ─────────────────────────────────────────────

class SavedLocationSerializer(serializers.ModelSerializer):
    owner = serializers.SerializerMethodField()

    class Meta:
        model = SavedLocation
        fields = (
            'id', 'name', 'description', 'location_type',
            'latitude', 'longitude', 'altitude_m', 'radius_km',
            'is_primary', 'is_public', 'is_active',
            'iot_device_id', 'owner',
            'created_at', 'updated_at'
        )
        read_only_fields = ('id', 'owner', 'created_at', 'updated_at')

    def get_owner(self, obj):
        return obj.get_owner_display()

    def validate(self, attrs):
        lat = attrs.get('latitude')
        lon = attrs.get('longitude')
        if lat and not (-90 <= lat <= 90):
            raise serializers.ValidationError({"latitude": "Must be between -90 and 90."})
        if lon and not (-180 <= lon <= 180):
            raise serializers.ValidationError({"longitude": "Must be between -180 and 180."})
        return attrs


# ─────────────────────────────────────────────
# ACTIVITY LOG
# ─────────────────────────────────────────────

class ActivityLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AccountActivityLog
        fields = (
            'id', 'action', 'description',
            'organization', 'ip_address', 'timestamp'
        )
        read_only_fields = fields