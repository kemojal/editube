"""Add community forum schema and tables

Revision ID: cdb173bf8fa7
Revises: cceb2cefce66
Create Date: 2026-04-19 13:39:33.136247

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'cdb173bf8fa7'
down_revision = 'cceb2cefce66'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE SCHEMA IF NOT EXISTS community")

    op.create_table('categories',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('slug', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('description', sa.String(), nullable=True),
    sa.Column('color', sa.String(), server_default='#8B5CF6', nullable=True),
    sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    schema='community'
    )
    op.create_index(op.f('ix_community_categories_id'), 'categories', ['id'], unique=False, schema='community')
    op.create_index(op.f('ix_community_categories_slug'), 'categories', ['slug'], unique=True, schema='community')
    op.create_table('posts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('status', sa.String(), server_default='open', nullable=False),
    sa.Column('view_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('category_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['category_id'], ['community.categories.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    schema='community'
    )
    op.create_index(op.f('ix_community_posts_category_id'), 'posts', ['category_id'], unique=False, schema='community')
    op.create_index(op.f('ix_community_posts_id'), 'posts', ['id'], unique=False, schema='community')
    op.create_index(op.f('ix_community_posts_status'), 'posts', ['status'], unique=False, schema='community')
    op.create_index(op.f('ix_community_posts_user_id'), 'posts', ['user_id'], unique=False, schema='community')
    
    # Check if device_push_tokens exists to avoid failure if another migration added it
    # But since it was generated here, we can create it
    op.create_table('device_push_tokens',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('token', sa.String(), nullable=False),
    sa.Column('platform', sa.String(), nullable=False),
    sa.Column('device_name', sa.String(), nullable=True),
    sa.Column('app_version', sa.String(), nullable=True),
    sa.Column('enabled', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token', name='uq_device_push_token_token')
    )
    op.create_index(op.f('ix_device_push_tokens_id'), 'device_push_tokens', ['id'], unique=False)
    op.create_index(op.f('ix_device_push_tokens_token'), 'device_push_tokens', ['token'], unique=False)
    op.create_index(op.f('ix_device_push_tokens_user_id'), 'device_push_tokens', ['user_id'], unique=False)
    
    op.create_table('comments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('post_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('parent_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['parent_id'], ['community.comments.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['post_id'], ['community.posts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    schema='community'
    )
    op.create_index(op.f('ix_community_comments_id'), 'comments', ['id'], unique=False, schema='community')
    op.create_index(op.f('ix_community_comments_parent_id'), 'comments', ['parent_id'], unique=False, schema='community')
    op.create_index(op.f('ix_community_comments_post_id'), 'comments', ['post_id'], unique=False, schema='community')
    op.create_index(op.f('ix_community_comments_user_id'), 'comments', ['user_id'], unique=False, schema='community')
    op.create_table('votes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('post_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['post_id'], ['community.posts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('post_id', 'user_id', name='uq_forum_votes_post_user'),
    schema='community'
    )
    op.create_index(op.f('ix_community_votes_id'), 'votes', ['id'], unique=False, schema='community')
    op.create_index(op.f('ix_community_votes_post_id'), 'votes', ['post_id'], unique=False, schema='community')
    op.create_index(op.f('ix_community_votes_user_id'), 'votes', ['user_id'], unique=False, schema='community')


def downgrade():
    op.drop_index(op.f('ix_community_votes_user_id'), table_name='votes', schema='community')
    op.drop_index(op.f('ix_community_votes_post_id'), table_name='votes', schema='community')
    op.drop_index(op.f('ix_community_votes_id'), table_name='votes', schema='community')
    op.drop_table('votes', schema='community')
    op.drop_index(op.f('ix_community_comments_user_id'), table_name='comments', schema='community')
    op.drop_index(op.f('ix_community_comments_post_id'), table_name='comments', schema='community')
    op.drop_index(op.f('ix_community_comments_parent_id'), table_name='comments', schema='community')
    op.drop_index(op.f('ix_community_comments_id'), table_name='comments', schema='community')
    op.drop_table('comments', schema='community')
    op.drop_index(op.f('ix_device_push_tokens_user_id'), table_name='device_push_tokens')
    op.drop_index(op.f('ix_device_push_tokens_token'), table_name='device_push_tokens')
    op.drop_index(op.f('ix_device_push_tokens_id'), table_name='device_push_tokens')
    op.drop_table('device_push_tokens')
    op.drop_index(op.f('ix_community_posts_user_id'), table_name='posts', schema='community')
    op.drop_index(op.f('ix_community_posts_status'), table_name='posts', schema='community')
    op.drop_index(op.f('ix_community_posts_id'), table_name='posts', schema='community')
    op.drop_index(op.f('ix_community_posts_category_id'), table_name='posts', schema='community')
    op.drop_table('posts', schema='community')
    op.drop_index(op.f('ix_community_categories_slug'), table_name='categories', schema='community')
    op.drop_index(op.f('ix_community_categories_id'), table_name='categories', schema='community')
    op.drop_table('categories', schema='community')
    
    op.execute("DROP SCHEMA IF EXISTS community CASCADE")
